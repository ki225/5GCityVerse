import time


class TimeUtils:
    @staticmethod
    def now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def epoch_millis() -> int:
        return int(time.time() * 1000)
