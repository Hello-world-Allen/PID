import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import grequests
from influxdb_client import InfluxDBClient
from simple_pid import PID

INFLUX_URL = "http://192.168.7.139:8086"
INFLUX_TOKEN = "lsTrnTXSD028sM0X5b_tVU34enPl0yzRftfEdYiaB_vWE8PM0qL_HahsJ8cxd4vuGJDjMUe3NxVzHLnbObheoA=="   # <-- 改成你的 token
INFLUX_ORG = "NTHU"
INFLUX_BUCKET = "test_bucket"

MEASUREMENT = "AC_data"
AGG_EVERY = "30s"
LOOKBACK = "-30m"

API_IP = "192.168.7.240"
BASE_URL = f"http://{API_IP}:8000/control/ac/"

AC_IDS = (1, 2, 3)
INTERVAL_S = 30

# =========================
# 正常 PID 設定值
# =========================
SP_SUPPLY = 23.0
SP_RETURN = 34.0

# =========================
# 定時過熱實驗設定
# =========================
# 實驗控制流程：
#
# 1. 正常運轉：
#       供氣溫度設定值 = 23°C
#       回氣溫度設定值 = 34°C
#
# 2. 每天使用台灣時間進行兩次過熱實驗：
#       上午 10:00 ~ 11:00
#       下午 15:00 ~ 16:00
#    在實驗時段內，AC1、AC2 與 AC3 都會各自使用：
#       供氣溫度設定值 = 28°C
#       回氣溫度設定值 = 38°C
#
# 3. 實驗期間每 30 秒分別檢查 AC1、AC2 與 AC3 的實際溫度。
#    對於每一台 AC，只要該台發生以下任一條件：
#       Supply_air_T > 30°C
#       return_air_T > 40°C
#    就只停止該台 AC 的過熱實驗，並立即將該台 AC 恢復成
#    正常設定值 23 / 34°C。
#    其他 AC 若尚未超過門檻，則繼續維持 28 / 38°C 的過熱實驗。
#
# 4. 某一台 AC 一旦觸發溫度門檻，該台 AC 在當次實驗時段剩餘時間內
#    都維持正常設定值，不會在下一個 30 秒控制週期重新進入過熱模式。
#
# 5. 若整個實驗時段內都沒有觸發門檻，則在 11:00 或 16:00 時，
#    AC1、AC2 與 AC3 都會自動恢復正常設定值 23 / 34°C。
#
# 6. 若實驗期間某一台 AC 的溫度資料讀取不到，為了安全起見，
#    只停止該台 AC 的過熱實驗並恢復正常設定值；其他 AC 不受影響。
# =========================
OVERHEAT_SP_SUPPLY = 28.0
OVERHEAT_SP_RETURN = 38.0

OVERHEAT_SUPPLY_LIMIT = 30.0
OVERHEAT_RETURN_LIMIT = 40.0

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

EXPERIMENT_WINDOWS = (
    ("morning", dt_time(10, 0), dt_time(11, 0)),
    ("afternoon", dt_time(15, 0), dt_time(16, 0)),
)

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


# 每台 AC 都使用自己的 PID 內部狀態，避免積分項與歷史狀態互相干擾。
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


def get_active_experiment(now: datetime):
    """判斷目前是否位於實驗時段，若是則回傳實驗名稱，否則回傳 None。"""
    current_time = now.time().replace(tzinfo=None)

    for name, start_time, end_time in EXPERIMENT_WINDOWS:
        if start_time <= current_time < end_time:
            return name

    return None


def get_overheat_cutoff_reason(ac_id: int, supply_t, return_t):
    """檢查單一 AC 是否應停止過熱實驗，若需要則回傳原因。"""
    if supply_t is None or return_t is None:
        return f"AC{ac_id} 溫度資料缺失"

    if supply_t > OVERHEAT_SUPPLY_LIMIT:
        return (
            f"AC{ac_id} 供氣溫度={supply_t:.1f}C "
            f"> {OVERHEAT_SUPPLY_LIMIT:.1f}C"
        )

    if return_t > OVERHEAT_RETURN_LIMIT:
        return (
            f"AC{ac_id} 回氣溫度={return_t:.1f}C "
            f"> {OVERHEAT_RETURN_LIMIT:.1f}C"
        )

    return None


def set_pid_setpoints(ac_id: int, supply_sp: float, return_sp: float):
    """只更新指定 AC 的供氣與回氣 PID 設定值。"""
    pid_ball, pid_fan = PID_CONTROLLERS[ac_id]
    pid_ball.setpoint = supply_sp
    pid_fan.setpoint = return_sp


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

    # AC1、AC2 與 AC3 的控制命令會在同一批 request 中送出。
    grequests.map(
        reqs,
        size=len(reqs),
    )


def main():
    # 記錄本次程式執行期間，哪些 AC 已在某個實驗時段觸發停止條件。
    # key 格式：(日期, 實驗名稱, AC 編號)
    # 例如：(2026-09-14, "morning", 1)
    stopped_ac_experiments = set()

    while True:
        t0 = time.time()
        now = datetime.now(TAIPEI_TZ)

        # 每個控制週期只查詢一次資料庫。
        df = query_to_dataframe()

        # 先分別讀取 AC1、AC2 與 AC3 的最新供氣與回氣溫度。
        readings = {
            ac_id: get_ac_latest_temps(df, ac_id)
            for ac_id in AC_IDS
        }

        active_experiment = get_active_experiment(now)
        commands = {}

        for ac_id in AC_IDS:
            supply_t, return_t = readings[ac_id]

            # 每台 AC 各自判斷是否仍允許執行目前的過熱實驗。
            experiment_key = (
                (now.date(), active_experiment, ac_id)
                if active_experiment is not None
                else None
            )

            experiment_allowed = (
                active_experiment is not None
                and experiment_key not in stopped_ac_experiments
            )

            # 只檢查該台 AC 自己的溫度門檻。
            if experiment_allowed:
                cutoff_reason = get_overheat_cutoff_reason(
                    ac_id,
                    supply_t,
                    return_t,
                )

                if cutoff_reason is not None:
                    stopped_ac_experiments.add(experiment_key)
                    experiment_allowed = False

                    print(
                        f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"[安全切換] {cutoff_reason} -> "
                        f"只將 AC{ac_id} 恢復為 "
                        f"SupplySP={SP_SUPPLY:.1f}C, "
                        f"ReturnSP={SP_RETURN:.1f}C"
                    )

            # 每台 AC 分別決定目前要使用過熱設定值或正常設定值。
            if experiment_allowed:
                current_supply_sp = OVERHEAT_SP_SUPPLY
                current_return_sp = OVERHEAT_SP_RETURN
                mode = f"過熱實驗:{active_experiment}"
            else:
                current_supply_sp = SP_SUPPLY
                current_return_sp = SP_RETURN

                if active_experiment is not None:
                    mode = f"恢復模式:{active_experiment}"
                else:
                    mode = "正常模式"

            # 只更新目前這台 AC 的 PID 設定值，不影響其他 AC。
            set_pid_setpoints(
                ac_id,
                current_supply_sp,
                current_return_sp,
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
                f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[AC{ac_id}] "
                f"模式={mode} | "
                f"SupplySP={current_supply_sp:.1f}C "
                f"ReturnSP={current_return_sp:.1f}C | "
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
