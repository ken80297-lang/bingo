from abc import ABC, abstractmethod


class BaseProvider(ABC):

    @abstractmethod
    def fetch_latest(self):
        """取得最新一期"""
        pass

    @abstractmethod
    def fetch_history(self, days=7):
        """取得歷史資料"""
        pass