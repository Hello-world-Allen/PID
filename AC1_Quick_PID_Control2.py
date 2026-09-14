import os
import time
import pandas as pd
import grequests
from influxdb_client import InfluxDBClient
from simple_pid import PID

INFLUX_URL = "http://192.168.7.139:8086"
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = "NTHU"
INFLUX_BUCKET = "test_bucket"

MEASUREMENT = "AC_data"
AGG_EVERY = "30s"
LOOKBACK = "-30m"

API_IP = "192.168.7.240"
BASE_URL = f"http://{API_IP}:8000/control/ac/"

AC_IDS = (1, 2)
INTERVAL_S = 30

SP_SUPPLY = 23.0
SP_RETURN = 34.0
OUT_MIN, OUT_MAX = 30, 100

ENABLE_SUPPLY_GT = 21.5
ENABLE_RETURN_GT = 31.0
HOLD_OUTPUT = 30.0


def make_pid_pair():
    pid_ball = PID(-4.0, -0.05, 0.0, setpoint=SP_SUPPLY)
    pid_ball.output_limits = (OUT_MIN, OUT_MAX)

    pid_fan = PID(-4.0, -0.05, 0.0, setpoint=SP_RETURN)
    pid_fan.output_limits = (OUT_MIN, OUT_MAX)

    return pid_ball, pid_fan


# 每台 AC 都要有自己的 PID 內部狀態，避免積分項互相干擾
PID_CONTROLLERS = {
    ac_id: make_pid_pair()
    for ac_id in AC_IDS
}


def query_to_dataframe():
    flux_query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {LOOKBACK})
      |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
      |> aggregateWindow(every: {AGG_EVERY}, fn: mean, createEmpty: false)
      |> yield(name: "mean")
    """

    with InfluxDBClient(
        url=INFLUX_URL,
        token=INFLUX_TOKEN,
        org=INFLUX_ORG,
    ) as client:
        query_api = client.query_api()
        result = query_api.query(org=INFLUX_ORG, query=flux_query)

    data = []
    for table in result:
        for record in table.records:
            data.append({
                "_time": record.get_time(),
                "_field": record.get_field(),
                "_value": record.get_value(),
                "AC_number": record.values.get("AC_number"),
            })

    df = pd.DataFrame(data)

    if df.empty:
        return df

    df["_time"] = pd.to_datetime(df["_time"], format="ISO8601")
    df["_time_local"] = df["_time"].dt.tz_convert("Asia/Taipei")

    return df


def get_ac_latest_temps(df_long: pd.DataFrame, ac_id: int):
    if df_long.empty:
        return None, None

    df_ac = df_long[
        df_long["AC_number"].astype(str) == str(ac_id)
    ]

    if df_ac.empty:
        return None, None

    wide = (
        df_ac
        .pivot(
            index="_time_local",
            columns="_field",
            values="_value",
        )
        .sort_index()
    )

    if wide.empty:
        return None, None

    supply_raw = (
        wide["Supply_air_T"].iloc[-1]
        if "Supply_air_T" in wide.columns
        else None
    )

    return_raw = (
        wide["return_air_T"].iloc[-1]
        if "return_air_T" in wide.columns
        else None
    )

    supply = (
        float(supply_raw) / 10.0
        if supply_raw is not None
        else None
    )

    ret = (
        float(return_raw) / 10.0
        if return_raw is not None
        else None
    )

    return supply, ret


def send_commands(commands):
    reqs = []

    for ac_id, (valve_cmd, fan_cmd) in commands.items():
        valve_cmd_i = int(
            round(max(0, min(100, valve_cmd)))
        )

        fan_cmd_i = int(
            round(max(0, min(100, fan_cmd)))
        )

        reqs.append(
            grequests.put(
                BASE_URL
                + f"{ac_id}/ball_valve_opening"
                + f"?opening_percent={valve_cmd_i}"
            )
        )

        reqs.append(
            grequests.put(
                BASE_URL
                + f"{ac_id}/fan_speed"
                + f"?speed={fan_cmd_i}"
            )
        )

    # AC1 / AC2 的 4 個命令在同一批 request 中送出
    grequests.map(
        reqs,
        size=len(reqs),
    )


def main():
    while True:
        t0 = time.time()

        # 每個週期只查一次資料庫
        df = query_to_dataframe()

        commands = {}

        for ac_id in AC_IDS:
            supply_t, return_t = get_ac_latest_temps(
                df,
                ac_id,
            )

            valve_cmd = HOLD_OUTPUT
            fan_cmd = HOLD_OUTPUT

            pid_ball, pid_fan = PID_CONTROLLERS[ac_id]

            if (
                supply_t is not None
                and supply_t > ENABLE_SUPPLY_GT
            ):
                valve_cmd = float(
                    pid_ball(supply_t)
                )

            if (
                return_t is not None
                and return_t > ENABLE_RETURN_GT
            ):
                fan_cmd = float(
                    pid_fan(return_t)
                )

            commands[ac_id] = (
                valve_cmd,
                fan_cmd,
            )

            print(
                f"[AC{ac_id}] "
                f"Supply={supply_t}C "
                f"Return={return_t}C | "
                f"ValveCmd={valve_cmd:.1f}% "
                f"FanCmd={fan_cmd:.1f}%"
            )

        send_commands(commands)

        elapsed = time.time() - t0

        time.sleep(
            max(
                0.0,
                INTERVAL_S - elapsed,
            )
        )


if __name__ == "__main__":
    main()
