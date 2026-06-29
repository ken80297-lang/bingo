from .base import BaseProvider


class KuaiShouProvider(BaseProvider):

    def fetch_latest(self):
        print("取得最新一期")
        return None

    def fetch_history(self, days=7):
        print(f"取得最近 {days} 天")
        return []