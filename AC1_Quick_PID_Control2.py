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

AC_IDS = (1, 2)
INTERVAL_S = 30

# =========================
# Normal PID setpoints
# =========================
SP_SUPPLY = 23.0
SP_RETURN = 34.0

# =========================
# Scheduled overheating experiment
# =========================
# Experiment control flow:
#
# 1. Normal operation:
#       Supply SP = 23 C
#       Return SP = 34 C
#
# 2. Every day, two experiment windows are scheduled using Asia/Taipei time:
#       10:00 ~ 11:00
#       15:00 ~ 16:00
#    During the experiment window, both AC1 and AC2 use:
#       Supply SP = 28 C
#       Return SP = 38 C
#
# 3. During an experiment, the actual temperatures of BOTH ACs are checked
#    every control cycle. If ANY one of the following occurs:
#       AC1/AC2 Supply_air_T > 30 C
#       AC1/AC2 return_air_T > 40 C
#    the experiment is immediately stopped and BOTH ACs return to the
#    normal setpoints (23 / 34 C).
#
# 4. Once a safety cutoff is triggered, that experiment window is latched
#    off for the rest of the current window. It will NOT re-enter the
#    overheating setpoints on the next 30-second cycle.
#
# 5. If no cutoff occurs, the experiment ends automatically when the
#    scheduled window ends, and the normal setpoints are restored.
#
# 6. If temperature data for either AC is missing during an experiment,
#    the experiment is also stopped for that window and normal setpoints
#    are used as a fail-safe.
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


# Each AC has its own PID internal state so integral/history do not interfere.
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
    """Return the current experiment name, or None outside experiment windows."""
    current_time = now.time().replace(tzinfo=None)

    for name, start_time, end_time in EXPERIMENT_WINDOWS:
        if start_time <= current_time < end_time:
            return name

    return None


def get_overheat_cutoff_reason(readings):
    """Return a cutoff reason if any AC exceeds the experiment safety limits."""
    for ac_id, (supply_t, return_t) in readings.items():
        if supply_t is None or return_t is None:
            return f"AC{ac_id} temperature data missing"

        if supply_t > OVERHEAT_SUPPLY_LIMIT:
            return (
                f"AC{ac_id} Supply={supply_t:.1f}C "
                f"> {OVERHEAT_SUPPLY_LIMIT:.1f}C"
            )

        if return_t > OVERHEAT_RETURN_LIMIT:
            return (
                f"AC{ac_id} Return={return_t:.1f}C "
                f"> {OVERHEAT_RETURN_LIMIT:.1f}C"
            )

    return None


def set_all_pid_setpoints(supply_sp: float, return_sp: float):
    """Apply the same experiment/normal setpoints to AC1 and AC2 PID objects."""
    for pid_ball, pid_fan in PID_CONTROLLERS.values():
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

    # AC1 / AC2 commands are sent in the same request batch.
    grequests.map(
        reqs,
        size=len(reqs),
    )


def main():
    # Stores experiment windows that have already hit a cutoff during this run.
    # Key format: (date, experiment_name), e.g. (2026-09-14, "morning").
    stopped_experiments = set()

    while True:
        t0 = time.time()
        now = datetime.now(TAIPEI_TZ)

        # Query the database once per control cycle.
        df = query_to_dataframe()

        # Read AC1 and AC2 temperatures before deciding the current setpoints.
        readings = {
            ac_id: get_ac_latest_temps(df, ac_id)
            for ac_id in AC_IDS
        }

        active_experiment = get_active_experiment(now)
        experiment_key = (
            (now.date(), active_experiment)
            if active_experiment is not None
            else None
        )

        experiment_allowed = (
            active_experiment is not None
            and experiment_key not in stopped_experiments
        )

        cutoff_reason = None

        if experiment_allowed:
            cutoff_reason = get_overheat_cutoff_reason(readings)

            if cutoff_reason is not None:
                stopped_experiments.add(experiment_key)
                experiment_allowed = False

                print(
                    f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[SAFETY CUTOFF] {cutoff_reason} -> "
                    f"restore SP to Supply={SP_SUPPLY:.1f}C, "
                    f"Return={SP_RETURN:.1f}C"
                )

        if experiment_allowed:
            current_supply_sp = OVERHEAT_SP_SUPPLY
            current_return_sp = OVERHEAT_SP_RETURN
            mode = f"OVERHEAT:{active_experiment}"
        else:
            current_supply_sp = SP_SUPPLY
            current_return_sp = SP_RETURN

            if active_experiment is not None:
                mode = f"RECOVERY:{active_experiment}"
            else:
                mode = "NORMAL"

        # Update both AC PID objects with the selected setpoints.
        set_all_pid_setpoints(
            current_supply_sp,
            current_return_sp,
        )

        print(
            f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Mode={mode} | "
            f"SupplySP={current_supply_sp:.1f}C "
            f"ReturnSP={current_return_sp:.1f}C"
        )

        commands = {}

        for ac_id in AC_IDS:
            supply_t, return_t = readings[ac_id]

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
