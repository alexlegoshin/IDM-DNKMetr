"""Подбор Kp/Ki/slew_rate для SoftwarePIController на модели нагрузки (без железа).

Запускать при первой настройке или если поменялась нагрузка/датчик:

    python tune_current_loop.py

Печатает ток по шагам и момент, когда is_stable() срабатывает. Если не
стабилизируется за отведённое число шагов — увеличивай ki или уменьшай
deadband-требования; если появляются колебания — уменьшай kp/ki.

Здесь только числовая модель ~800-Ом нагрузки для проверки, что сам алгоритм
регулирования устойчив, прежде чем выходить к реальным приборам (см. README.md).
"""

from current_control import SoftwarePIController, _window_is_stable
from drivers import FakeMultimeter, FakePowerSupply


def simulate(kp, ki, slew_rate, dt=0.2, steps=60, load_ohms=800.0, noise=0.0, verbose=True):
    supply = FakePowerSupply()
    dmm = FakeMultimeter(supply, load_ohms=load_ohms, noise=noise)
    supply.enable_output(True)
    ctrl = SoftwarePIController(
        supply, dmm, setpoint=0.010,
        kp=kp, ki=ki, dt=dt, deadband=0.0002,
        slew_rate=slew_rate,
    )
    ctrl._wait_tick = lambda: None  # ускоряем симуляцию, dt остаётся в математике

    window = []
    for i in range(steps):
        current = ctrl.step()
        if verbose:
            print(f"{i:3d}  I={current*1000:7.4f} mA  U={supply.voltage:7.3f} V")
        if _window_is_stable(window := (window + [current])[-5:], 5,
                              ctrl.deadband * 2, ctrl.setpoint, ctrl.deadband * 3):
            print(f"STABLE at step {i} (~{i*dt:.1f} s): I={current*1000:.4f} mA")
            return i * dt
    print("did not stabilize within the step budget")
    return None


if __name__ == "__main__":
    simulate(kp=100.0, ki=1500.0, slew_rate=25.0)
