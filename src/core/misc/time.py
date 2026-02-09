import random
import time


def pendulum_sleep(base_seconds: float, swing_range: float):
    """
    对称钟摆随机睡眠
    :param base_seconds: 基准睡眠时长（秒，可浮点）
    :param swing_range: 波动范围（秒，可浮点，左右各波动该值）
    """
    # 计算睡眠时长的上下限
    min_sleep = max(0, base_seconds - swing_range)  # 避免睡眠时长为负数
    max_sleep = base_seconds + swing_range
    # 生成随机睡眠时长（浮点数，更细腻）
    sleep_seconds = random.uniform(min_sleep, max_sleep)
    # 执行休眠
    print(f"钟摆休眠：{sleep_seconds:.2f}秒（基准：{base_seconds}秒，波动：±{swing_range}秒）")
    time.sleep(sleep_seconds)
