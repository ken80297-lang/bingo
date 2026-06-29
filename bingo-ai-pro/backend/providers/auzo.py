from .base import BaseProvider


class AuzoProvider(BaseProvider):

    def fetch_latest(self):
        return None

    def fetch_history(self, days=7):
        return []