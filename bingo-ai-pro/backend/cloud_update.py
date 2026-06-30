from db import fetch_latest_draws, save_draws


def main():
    print("開始雲端更新...")

    draws = fetch_latest_draws()

    if not draws:
        print("沒有取得資料")
        return

    print(f"取得 {len(draws)} 期資料")
    print(f"最新期數：{draws[0].issue}")

    added = save_draws(draws)

    print(f"新增 {added} 期")
    print("雲端更新完成")


if __name__ == "__main__":
    main()