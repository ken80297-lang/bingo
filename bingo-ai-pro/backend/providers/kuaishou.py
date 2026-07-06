import requests

from .base import BaseProvider


API_URL = "https://bingo2.kuaishou1688.com/api/get_data"


def fetch_kuaishou_data(count=None):
    response = requests.post(
        API_URL,
        json={"count": count},
        timeout=20,
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://bingo2.kuaishou1688.com",
            "referer": "https://bingo2.kuaishou1688.com/",
            "user-agent": "Mozilla/5.0",
        },
    )

    response.raise_for_status()
    return response.json()


class KuaiShouProvider(BaseProvider):
    def fetch_latest(self):
        return fetch_kuaishou_data()

    def fetch_history(self, days=7):
        return fetch_kuaishou_data(count=days)
