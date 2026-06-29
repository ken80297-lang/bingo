from providers.kuaishou import KuaiShouProvider
from providers.auzo import AuzoProvider
from providers.official import OfficialProvider


class DataHub:
    def __init__(self):
        self.providers = [
            KuaiShouProvider(),
            AuzoProvider(),
            OfficialProvider(),
        ]

    def fetch_latest(self):
        for provider in self.providers:
            try:
                data = provider.fetch_latest()
                if data:
                    print(f"成功：{provider.__class__.__name__}")
                    return data
            except Exception as e:
                print(f"{provider.__class__.__name__} 失敗：{e}")

        return None