import subprocess
import sys
import os

DEFAULT_BOT_IDS = [1, 2, 3, 4, 5]

def parse_bot_ids():
    raw = os.getenv("ACTIVE_BOTS", "").strip()
    if not raw:
        return DEFAULT_BOT_IDS

    bot_ids = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            bot_ids.append(int(chunk))
        except ValueError:
            print(f"Peringatan: bot id '{chunk}' tidak valid, dilewati.")

    return bot_ids or DEFAULT_BOT_IDS

def main():
    # Pastikan script.py ada
    if not os.path.exists("script.py"):
        print("Error: script.py tidak ditemukan di folder ini.")
        return

    bot_ids = parse_bot_ids()
    processes = []
    print(f"Bot aktif: {', '.join(str(i) for i in bot_ids)}")

    for i in bot_ids:
        # Cek apakah file key ada (opsional)
        key_file = f"key_{i}.txt"
        has_env_key = bool(os.getenv(f"KEY_{i}", "").strip())
        if not has_env_key and not os.path.exists(key_file):
            print(f"Peringatan: KEY_{i} / {key_file} tidak ditemukan. Bot {i} mungkin gagal.")

        # Jalankan proses
        p = subprocess.Popen([sys.executable, "script.py", str(i)])
        processes.append(p)
        print(f"Bot {i} dimulai dengan PID {p.pid}")

    print("\nSemua bot berjalan. Tekan Ctrl+C untuk menghentikan.\n")

    try:
        # Tunggu semua proses selesai (tidak akan selesai karena infinite loop)
        for p in processes:
            p.wait()
    except KeyboardInterrupt:
        print("\nMenghentikan semua bot...")
        for p in processes:
            p.terminate()
        # Beri waktu untuk terminasi
        for p in processes:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        print("Semua bot dihentikan.")

if __name__ == "__main__":
    main()
