import time
import pandas as pd
import grequests
from influxdb_client import InfluxDBClient
from simple_pid import PID

# =========================
# InfluxDB config (照 main_PID_AC_alle_stepT.py 結構)  [1](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/Documents/Microsoft%20Copilot%20Chat%20%E6%AA%94%E6%A1%88/main_PID_AC_alle_stepT.py)
# =========================
INFLUX_URL = "http://192.168.7.139:8086"
INFLUX_TOKEN = "lsTrnTXSD028sM0X5b_tVU34enPl0yzRftfEdYiaB_vWE8PM0qL_HahsJ8cxd4vuGJDjMUe3NxVzHLnbObheoA=="   # <-- 改成你的 token
INFLUX_ORG = "NTHU"
INFLUX_BUCKET = "test_bucket"

MEASUREMENT = "AC_data"
AGG_EVERY = "30s"
LOOKBACK = "-30m"

# =========================
# Control API config (照 空調控制.pptx 第4頁)  [2](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/_layouts/15/Doc.aspx?sourcedoc=%7B1A4C751F-D36C-4D88-AD14-491A7A89BA1E%7D&file=%E7%A9%BA%E8%AA%BF%E6%8E%A7%E5%88%B6.pptx&action=edit&mobileredirect=true)
# =========================
API_IP = "192.168.7.240"
BASE_URL = f"http://{API_IP}:8000/control/ac/"

AC_ID = 1
INTERVAL_S = 30

# =========================
# PID config (你的規格)
# =========================
SP_SUPPLY = 23.0
SP_RETURN = 34.0
OUT_MIN, OUT_MAX = 30, 100

ENABLE_SUPPLY_GT = 21.5
ENABLE_RETURN_GT = 31.0
HOLD_OUTPUT = 30.0

# 快速驗證用：先給保守預設，可再依現場調參
pid_ball = PID(-4.0, -0.05, 0.0, setpoint=SP_SUPPLY)
pid_ball.output_limits = (OUT_MIN, OUT_MAX)

pid_fan = PID(-4.0, -0.05, 0.0, setpoint=SP_RETURN)
pid_fan.output_limits = (OUT_MIN, OUT_MAX)

def query_to_dataframe():
    """照 main_PID_AC_alle_stepT.py：Influx query -> long dataframe  [1](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/Documents/Microsoft%20Copilot%20Chat%20%E6%AA%94%E6%A1%88/main_PID_AC_alle_stepT.py)"""
    flux_query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {LOOKBACK})
      |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
      |> aggregateWindow(every: {AGG_EVERY}, fn: mean, createEmpty: false)
      |> yield(name: "mean")
    """
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        query_api = client.query_api()
        result = query_api.query(org=INFLUX_ORG, query=flux_query)

    data = []
    for table in result:
        for record in table.records:
            data.append({
                "_time": record.get_time(),
                "_field": record.get_field(),
                "_value": record.get_value(),
                "AC_number": record.values.get("AC_number")
            })
    df = pd.DataFrame(data)
    if df.empty:
        return df

    df["_time"] = pd.to_datetime(df["_time"], format="ISO8601")
    df["_time_local"] = df["_time"].dt.tz_convert("Asia/Taipei")
    return df

def get_ac1_latest_temps(df_long: pd.DataFrame):
    """
    取 AC1 最新 Supply_air_T / return_air_T
    參照 main_PID_AC_alle_stepT.py：pivot 成 wide df，取最後一筆，溫度/10  [1](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/Documents/Microsoft%20Copilot%20Chat%20%E6%AA%94%E6%A1%88/main_PID_AC_alle_stepT.py)
    """
    if df_long.empty:
        return None, None

    df1 = df_long[df_long["AC_number"].astype(str) == str(AC_ID)]
    if df1.empty:
        return None, None

    wide = df1.pivot(index="_time_local", columns="_field", values="_value").sort_index()
    if wide.empty:
        return None, None

    supply_raw = wide["Supply_air_T"].iloc[-1] if "Supply_air_T" in wide.columns else None
    return_raw = wide["return_air_T"].iloc[-1] if "return_air_T" in wide.columns else None

    # 你原程式對 Supply_air_T 做 /10，因此這裡沿用同樣縮放  [1](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/Documents/Microsoft%20Copilot%20Chat%20%E6%AA%94%E6%A1%88/main_PID_AC_alle_stepT.py)
    supply = float(supply_raw) / 10.0 if supply_raw is not None else None
    ret = float(return_raw) / 10.0 if return_raw is not None else None

    return supply, ret

def send_commands(valve_cmd: float, fan_cmd: float):
    """
    命令格式參照 空調控制.pptx 第4頁 API  [2](https://inventeccorp-my.sharepoint.com/personal/lee_jamescm_inventec_com/_layouts/15/Doc.aspx?sourcedoc=%7B1A4C751F-D36C-4D88-AD14-491A7A89BA1E%7D&file=%E7%A9%BA%E8%AA%BF%E6%8E%A7%E5%88%B6.pptx&action=edit&mobileredirect=true)
    """
    valve_cmd_i = int(round(max(0, min(100, valve_cmd))))
    fan_cmd_i = int(round(max(0, min(100, fan_cmd))))

    reqs = []
    reqs.append(grequests.put(BASE_URL + f"{AC_ID}/ball_valve_opening?opening_percent={valve_cmd_i}"))
    reqs.append(grequests.put(BASE_URL + f"{AC_ID}/fan_speed?speed={fan_cmd_i}"))
    grequests.map(reqs, size=2)

def main():
    while True:
        t0 = time.time()

        df = query_to_dataframe()
        supply_t, return_t = get_ac1_latest_temps(df)

        # 預設保持最低輸出
        valve_cmd = HOLD_OUTPUT
        fan_cmd = HOLD_OUTPUT

        # PID_ball 啟用條件：Supply_air_T > 23°C
        if (supply_t is not None) and (supply_t > ENABLE_SUPPLY_GT):
            valve_cmd = float(pid_ball(supply_t))

        # PID_fan 啟用條件：return_air_T > 38°C
        if (return_t is not None) and (return_t > ENABLE_RETURN_GT):
            fan_cmd = float(pid_fan(return_t))

        # 送命令
        send_commands(valve_cmd, fan_cmd)

        print(f"[AC{AC_ID}] Supply={supply_t}C Return={return_t}C | "
              f"ValveCmd={valve_cmd:.1f}% FanCmd={fan_cmd:.1f}%")

        # 固定週期
        elapsed = time.time() - t0
        time.sleep(max(0.0, INTERVAL_S - elapsed))

if __name__ == "__main__":
    main()
