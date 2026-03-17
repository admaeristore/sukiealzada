import os
import random
import sys
import time
import json
from datetime import datetime

import requests

if hasattr(sys.stdout, 'encoding') and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore

# ==================== KONFIGURASI ====================
BASE_URL = "https://cdn.moltyroyale.com/api"

AGENT_NAMES = [
    "KangBray_1", "KangBray_2", "KangBray_3", "KangBray_4", "KangBray_5"
]

# Data item statis
WEAPONS = {
    "Fist": {"atkBonus": 0, "range": 0, "damage": 5},
    "Knife": {"atkBonus": 5, "range": 0, "damage": 10},
    "Sword": {"atkBonus": 8, "range": 0, "damage": 13},
    "Katana": {"atkBonus": 21, "range": 0, "damage": 26},
    "Bow": {"atkBonus": 3, "range": 1, "damage": 8},
    "Pistol": {"atkBonus": 6, "range": 1, "damage": 11},
    "Sniper": {"atkBonus": 17, "range": 2, "damage": 22},
}

RECOVERY_ITEMS = {
    "Emergency Food": {"hpRestore": 20, "priority": 1},
    "Emergency rations": {"hpRestore": 20, "priority": 1},  # Variasi nama di dalam game
    "Bandage": {"hpRestore": 30, "priority": 2},
    "Medkit": {"hpRestore": 50, "priority": 3},
    "Energy Drink": {"epRestore": 5, "priority": 2},
}

UTILITY_ITEMS = {
    "Binoculars": {"effect": "vision+1", "priority": 10},
    "Megaphone": {"effect": "broadcast", "priority": 1},
}

# Thresholds
HP_CRITICAL = 65
HP_LOW = 80
HP_MEDIUM = 90
EP_MIN_TO_ATTACK = 2
TURNS_BEFORE_MOVE = 2
DAMAGE_PER_HIT_MIN = 1  # Turunkan agar tangan kosong tetap dianggap berguna
TEAM_AGENT_NAMES = set(AGENT_NAMES)

def log(bot_id, msg):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] [Bot-{bot_id}] {msg}", flush=True)

def structured_log(bot_id, tag, **fields):
    payload = " | ".join(f"{key}={value}" for key, value in fields.items())
    log(bot_id, f"{tag} | {payload}" if payload else tag)

# ==================== KELAS UTAMA ====================
class EliteAgent:
    def __init__(self, index):
        self.index = index
        self.name = AGENT_NAMES[index-1] if 1 <= index <= len(AGENT_NAMES) else f"Agent{index}"
        self.key_file = f"key_{index}.txt"
        self.api_key = self.load_api_key()
        self.headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        
        # Status game
        self.game_id = ""
        self.agent_id = ""
        self.game_start_time = 0.0
        self.last_game_status = ""
        self.last_game_id = ""
        
        # Pengetahuan peta
        self.known_regions = {}
        self.pending_deathzones = set()
        self.visited_regions = set()
        self.current_region_id = ""
        self.turns_in_region = 0
        self.region_first_visit = {}  # track kapan pertama kali visit
        
        # Status agent
        self.last_hp = None
        self.last_ep = None
        self.total_damage_taken = 0
        self.last_state_time = 0
        self.consecutive_errors = 0
        self.last_action_time = 0
        self.action_cooldown = {}
        self.last_region_id = None
        self.explore_count_in_region = 0
        self.min_state_interval = 0.6
        self.waiting_poll_interval = 5
        self.max_waiting_interval = 20
        self.last_create_attempt = 0.0
        # Desinkronisasi antar bot agar tidak burst request di detik yang sama
        self.turn_jitter = random.uniform(0.4, 2.2)
        self.free_action_spacing = random.uniform(0.18, 0.45)
        
        # Statistik
        self.total_games = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_moltz_earned = 0
        self.current_game_moltz = 0
        
        # Navigasi & Looting
        self.target_loot_region = None
        self.squad_file = "squad_sync.json"
        self.role = "carry" if self.index in (2, 3) else "support"
        self.focus_target_id = None
        self.focus_target_region = None
        self.focus_target_expires = 0.0

        try:
            res = requests.get(f"{BASE_URL}/accounts/me", headers=self.headers, timeout=5)
            if res.status_code == 200:
                data = res.json().get("data", {})
                self.total_wins = data.get("totalWins", 0)
                self.total_games = data.get("totalGames", 0)
                log(self.index, f"📊 Total Games: {self.total_games} | Total Wins: {self.total_wins}")
        except Exception:
            pass
            
        log(self.index, f"✅ Bot {self.name} initialized")

    # ==================== API KEY ====================
    def load_api_key(self):
        env_key = os.getenv(f"KEY_{self.index}", "").strip()
        if env_key:
            log(self.index, "🔑 API Key loaded from environment")
            return env_key

        if not os.path.exists(self.key_file):
            log(self.index, f"❌ ERROR: File {self.key_file} tidak ditemukan!")
            sys.exit(1)
        with open(self.key_file, "r") as f:
            key = f.read().strip()
        log(self.index, "🔑 API Key loaded")
        return key

    # ==================== SQUAD SYNC ====================
    def load_squad_sync(self):
        if not os.path.exists(self.squad_file):
            return {}
        try:
            with open(self.squad_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_squad_sync(self, payload):
        try:
            with open(self.squad_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
        except Exception:
            pass

    def update_squad_sync(self, state):
        me = state.get("self", {})
        curr = state.get("currentRegion", {})
        squad = self.load_squad_sync()
        squad[self.name] = {
            "game_id": self.game_id,
            "agent_id": self.agent_id,
            "region_id": me.get("regionId") or curr.get("id") or "",
            "hp": me.get("hp", 100),
            "ep": me.get("ep", 0),
            "weapon": (me.get("equippedWeapon") or {}).get("name", "Fist"),
            "updated_at": time.time(),
        }
        self.save_squad_sync(squad)

    def get_squad_allies(self):
        squad = self.load_squad_sync()
        allies = []
        now = time.time()
        for name, info in squad.items():
            if name == self.name or name not in TEAM_AGENT_NAMES or not isinstance(info, dict):
                continue
            if info.get("game_id") != self.game_id:
                continue
            if now - float(info.get("updated_at", 0.0)) > 150:
                continue
            allies.append(info)
        return allies

    def squad_mode_active(self, state):
        me = state.get("self", {})
        my_region = str(me.get("regionId", ""))
        visible_allies = sum(
            1
            for ag in state.get("visibleAgents", [])
            if ag.get("id") != self.agent_id and ag.get("name") in TEAM_AGENT_NAMES
        )
        nearby_synced_allies = sum(
            1 for ally in self.get_squad_allies()
            if str(ally.get("region_id", "")) == my_region
        )
        return (visible_allies + nearby_synced_allies) >= 1

    def region_enemy_pressure(self, region_id, state):
        return sum(
            1
            for ag in state.get("visibleAgents", [])
            if ag.get("id") != self.agent_id and ag.get("regionId") == region_id
        )

    def is_late_game(self):
        if not self.game_start_time:
            return False
        # ~55 menit total game, anggap late game setelah 70% durasi berjalan.
        return (time.time() - self.game_start_time) >= (55 * 60 * 0.7)

    def should_avoid_region(self, region_id, state, hp, moltz_count):
        if not region_id:
            return True
        if not self.is_region_safe(region_id):
            return True

        enemy_pressure = self.region_enemy_pressure(region_id, state)
        if enemy_pressure >= 4:
            return True

        late_game = self.is_late_game()
        if enemy_pressure >= 3 and (late_game or hp < 75 or moltz_count >= 20):
            return True
        if enemy_pressure >= 2 and (hp < 55 or moltz_count >= 40):
            return True

        return False

    def choose_low_risk_region(self, region_ids, state, hp, moltz_count):
        best_region = None
        best_score = -999
        allies = self.get_squad_allies() if self.squad_mode_active(state) else []
        ally_regions = {str(ally.get("region_id", "")) for ally in allies}

        for region_id in region_ids:
            if self.should_avoid_region(region_id, state, hp, moltz_count):
                continue

            score = 0
            if region_id not in self.visited_regions:
                score += 10
            if region_id in ally_regions:
                score += 12
            if self.region_enemy_pressure(region_id, state) == 0:
                score += 18
            elif self.region_enemy_pressure(region_id, state) == 1:
                score += 4

            reg = self.known_regions.get(region_id, {})
            terrain = reg.get("terrain", "")
            if terrain == "hills":
                score += 8
            if terrain == "water":
                score -= 12

            if score > best_score:
                best_region = region_id
                best_score = score

        return best_region

    def get_visible_enemy_count(self, state):
        return sum(1 for ag in state.get("visibleAgents", []) if ag.get("id") != self.agent_id)

    def assess_combat_risk(self, state, target=None, dist=0):
        me = state.get("self", {})
        hp = int(me.get("hp", 100) or 100)
        ep = int(me.get("ep", 0) or 0)
        _, weapon_range = self.get_weapon_damage(me)
        enemy_count = self.get_visible_enemy_count(state)
        allies = self.get_squad_allies() if self.squad_mode_active(state) else []
        nearby_allies = sum(1 for ally in allies if ally.get("region_id") == me.get("regionId"))
        late_game = self.is_late_game()
        moltz_count = sum(
            1 for item in (me.get("inventory") or [])
            if item.get("category") == "currency" or "moltz" in str(item.get("name", "")).lower()
        )

        risk = 0
        if hp < 85:
            risk += (85 - hp) * 0.8
        if hp < 55:
            risk += 12
        if ep < 2:
            risk += 20
        elif ep < 4:
            risk += 8

        if enemy_count >= 2:
            risk += enemy_count * 10
        if nearby_allies > 0:
            risk -= nearby_allies * 7

        if late_game:
            risk += 10
        if moltz_count >= 20:
            risk += min(18, moltz_count // 3)

        if target:
            target_hp = int(target.get("hp", 100) or 100)
            eq_enemy = target.get("equippedWeapon") or {}
            enemy_range = WEAPONS.get(eq_enemy.get("name", "Fist"), {"range": 0})["range"]
            if target_hp <= 25:
                risk -= 20
            elif target_hp <= 45:
                risk -= 10
            if weapon_range > enemy_range and dist > 0:
                risk -= 12
            elif enemy_range >= weapon_range and dist <= enemy_range:
                risk += 12
            if target.get("regionId") == me.get("regionId") and enemy_count >= 2:
                risk += 15

        return max(0, int(risk))

    def should_engage(self, state, target=None, dist=0):
        risk = self.assess_combat_risk(state, target, dist)
        threshold = 50 if self.role == "carry" else 42
        if self.is_late_game():
            threshold -= 8
        return risk < threshold, risk

    def remember_focus_target(self, target):
        if not target:
            return
        self.focus_target_id = target.get("id")
        self.focus_target_region = target.get("regionId")
        self.focus_target_expires = time.time() + 95

    def get_focus_target(self, state):
        if not self.focus_target_id or time.time() > self.focus_target_expires:
            self.focus_target_id = None
            self.focus_target_region = None
            return None

        for group in (state.get("visibleAgents", []), state.get("visibleMonsters", [])):
            for unit in group:
                if unit.get("id") == self.focus_target_id:
                    return unit
        return None

    def item_priority_score(self, item):
        if not item:
            return 0
        name = str(item.get("name", ""))
        cat = item.get("category")
        if "moltz" in name.lower() or cat in ("currency", "money"):
            return 90
        if name in ["Sniper", "Sniper Rifle"]:
            return 120
        if name == "Binoculars":
            return 110
        if name == "Katana":
            return 100
        if name in ["Pistol", "Bow"]:
            return 85
        if name == "Energy Drink":
            return 75
        if name == "Medkit":
            return 95
        if name == "Bandage":
            return 70
        if name in ["Emergency Food", "Emergency rations"]:
            return 45
        return 0

    def choose_visible_loot_route(self, state):
        me = state.get("self", {})
        inventory = me.get("inventory") or []
        curr_region = me.get("regionId")
        connections = state.get("currentRegion", {}).get("connections", []) or []
        conn_ids = [(c.get("id") if isinstance(c, dict) else c) for c in connections]
        conn_ids = [str(cid) for cid in conn_ids if cid]

        if not conn_ids:
            return None

        has_sniper = any(i.get("name") in ["Sniper", "Sniper Rifle"] for i in inventory)
        has_katana = any(i.get("name") == "Katana" for i in inventory)
        has_binoc = any(i.get("name") == "Binoculars" for i in inventory)
        has_sidearm = any(i.get("name") in ["Bow", "Pistol"] for i in inventory)
        has_energy = any(i.get("name") == "Energy Drink" for i in inventory)

        best_region = None
        best_score = 0
        region_scores = {}

        for entry in state.get("visibleItems", []):
            region_id = str(entry.get("regionId", ""))
            item = entry.get("item") or {}
            if not region_id or region_id == curr_region:
                continue

            base = self.item_priority_score(item)
            name = item.get("name", "")
            if name in ["Sniper", "Sniper Rifle"] and has_sniper:
                base = 0
            elif name == "Katana" and has_katana:
                base = 0
            elif name == "Binoculars" and has_binoc:
                base = 0
            elif name in ["Bow", "Pistol"] and has_sidearm:
                base = 0
            elif name == "Energy Drink" and has_energy:
                base = 0

            if base <= 0:
                continue

            if region_id in conn_ids:
                score = base + 20
            else:
                score = base

            enemy_pressure = self.region_enemy_pressure(region_id, state)
            score -= enemy_pressure * 35

            if region_id in self.pending_deathzones:
                score -= 120

            region_scores[region_id] = region_scores.get(region_id, 0) + score

        for region_id, score in region_scores.items():
            if score > best_score:
                best_region = region_id
                best_score = score

        if not best_region or best_score < 60:
            return None

        if best_region in conn_ids and self.is_region_safe(best_region):
            return best_region, best_score

        for conn_id in conn_ids:
            reg = self.known_regions.get(conn_id, {})
            next_conns = reg.get("connections", []) or []
            if any(((nc.get("id") if isinstance(nc, dict) else nc) == best_region) for nc in next_conns):
                if self.is_region_safe(conn_id):
                    return conn_id, best_score - 10

        return None

    def choose_support_route(self, state):
        me = state.get("self", {})
        curr_region = me.get("regionId")
        connections = state.get("currentRegion", {}).get("connections", []) or []
        conn_ids = [(c.get("id") if isinstance(c, dict) else c) for c in connections]
        conn_ids = [str(cid) for cid in conn_ids if cid]
        if not self.squad_mode_active(state):
            return None
        allies = self.get_squad_allies()
        if not allies:
            return None

        best = None
        best_score = -999
        for ally in allies:
            ally_region = str(ally.get("region_id", ""))
            if not ally_region or ally_region == curr_region:
                continue
            if ally_region not in conn_ids:
                continue

            score = 0
            ally_hp = int(ally.get("hp", 100) or 100)
            if ally_hp < 60:
                score += 35
            if ally_hp < 40:
                score += 25

            enemy_pressure = self.region_enemy_pressure(ally_region, state)
            if enemy_pressure > 0:
                score += 15 + enemy_pressure * 8
            if enemy_pressure >= 3:
                score -= 35
            if ally_region in self.pending_deathzones:
                score -= 60
            if self.is_region_safe(ally_region):
                score += 10

            if score > best_score:
                best = ally_region
                best_score = score

        if best and best_score >= 25:
            return best, best_score
        return None

    # ==================== GAME JOIN ====================
    def recover_agent_id(self):
        try:
            res = requests.get(f"{BASE_URL}/games/{self.game_id}/state", timeout=10)
            if res.status_code == 200:
                agents = res.json()["data"].get("agents", [])
                for a in agents:
                    if a["name"] == self.name:
                        return a["id"]
        except:
            pass
        return None

    def find_and_join_game(self):
        log(self.index, f"🔍 Standby mencari game...")
        import json, os
        sync_file = "squad_sync.json"
        
        while not self.agent_id:
            try:
                # Cari game bebas, semuanya sekarang berlaku sebagai pemain Solo!
                res = requests.get(f"{BASE_URL}/games?status=waiting", timeout=10)
                if res.status_code != 200:
                    log(self.index, f"⚠️ Server Busy/Error: {res.status_code}. Tunggu 25 detik...")
                    time.sleep(25)
                    continue
                
                games = res.json().get("data", [])

                free_games = []
                for g in games:
                    if not isinstance(g, dict):
                        continue
                    raw_type = str(g.get("entryType") or g.get("type", "free")).lower()
                    if raw_type != "free":
                        continue

                    game_id = str(g.get("id", ""))
                    agent_count = int(g.get("agentCount", 0) or 0)
                    max_agents = int(g.get("maxAgents", 100) or 100)
                    if not game_id or agent_count >= max_agents:
                        continue
                    free_games.append(g)

                # Prioritaskan lobby yang paling penuh tapi masih menyisakan slot.
                free_games.sort(key=lambda g: int(g.get("agentCount", 0) or 0), reverse=True)
                target_game = free_games[0] if free_games else {}
                
                if not target_game:
                    now = time.time()
                    create_cooldown = 45.0 + random.uniform(0.0, 12.0)
                    if (now - self.last_create_attempt) < create_cooldown:
                        wait_left = max(6, int(create_cooldown - (now - self.last_create_attempt)))
                        log(self.index, f"⚠️ Tidak ada game FREE yang bisa dimasuki. Tunggu {wait_left}s sebelum create lagi...")
                        time.sleep(wait_left)
                        continue

                    log(self.index, f"⚠️ Tidak ada game FREE di waiting list. Membuat game baru...")
                    # Beri jeda acak agar beberapa bot tidak rebutan create di detik yang sama
                    time.sleep(random.uniform(2.0, 5.0))
                    self.last_create_attempt = time.time()
                    
                    # Coba create game
                    create_res = requests.post(
                        f"{BASE_URL}/games",
                        json={"hostName": f"{self.name}_room", "entryType": "free"},
                        headers=self.headers,
                        timeout=10,
                    )
                    if create_res.status_code in (200, 201):
                        new_data = create_res.json().get("data", {})
                        self.game_id = str(new_data.get("id", ""))
                        log(self.index, f"🆕 Berhasil membuat game baru: {new_data.get('name', self.game_id)}")
                    elif create_res.status_code == 409:
                        # 409 WAITING_GAME_EXISTS, berarti ada bot lain yang baru saja bikin game
                        log(self.index, f"♻️ Game baru sudah dibuat bot lain (409). Mencoba gabung...")
                        time.sleep(random.uniform(1.0, 3.0)) # Tunggu sekejap dan ulangi gabung
                        continue
                    elif create_res.status_code == 429:
                        log(self.index, f"⚠️ API Limit (429) saat create game, tunggu 15 detik...")
                        time.sleep(15)
                        continue
                    else:
                        log(self.index, f"❌ Gagal membuat game: HTTP {create_res.status_code}")
                        time.sleep(10)
                        continue
                else:
                    self.game_id = str(target_game.get("id", ""))
                    a_count = int(target_game.get("agentCount", 0) or 0)
                    max_agents = int(target_game.get("maxAgents", 100) or 100)
                    log(self.index, f"📌 Menyeleksi Game FREE tercepat: {target_game.get('name', self.game_id)} ({a_count}/{max_agents})")

                # === FILTER UNTUK MENYEBAR BOT ===
                # Agar tidak dalam 1 map, kita paksakan bot untuk memilih room secara random jika ada banyak room,
                # Atau hanya mendaftar jika jumlah agent di dalam room tersebut sangat sedikit (contoh < 15)
                # (Catatan sederhana: Karena logic Anda butuh game cepat mulai, kita biarkan logic asli join game yang sama.
                # Namun jika Anda benar-benar mau terpisah *beda map/beda server*, kita bisa memblokir bot join room
                # yang sama yang baru saja dimasuki bot lain).
                
                reg = requests.post(
                    f"{BASE_URL}/games/{self.game_id}/agents/register",
                    json={"name": self.name},
                    headers=self.headers,
                    timeout=10
                )

                if reg.status_code == 201:
                    data = reg.json()["data"]
                    self.agent_id = data["id"]
                    log(self.index, f"✅ BERHASIL DAFTAR! Agent ID: {self.agent_id}")
                    return
                    
                elif reg.status_code == 400:
                    err = reg.json().get("error", {})
                    code = err.get("code")
                    
                    if code in ("ONE_AGENT_PER_API_KEY", "ACCOUNT_ALREADY_IN_GAME"):
                        log(self.index, "🔄 Sudah terdaftar, recover ID...")
                        recovered_id = self.recover_agent_id()
                        if recovered_id:
                            self.agent_id = str(recovered_id)
                            log(self.index, f"✅ RECOVER BERHASIL! ID: {self.agent_id}")
                            return
                        else:
                            log(self.index, "⏳ Recover gagal, tunggu 30 detik...")
                            time.sleep(30)
                    elif code in ("GAME_ALREADY_STARTED", "MAX_AGENTS_REACHED"):
                        log(self.index, f"🚪 Telat gabung, gerbang lobby sudah tertutup / penuh! Menunggu sesi selanjutnya...")
                        self.last_game_id = self.game_id
                        self.game_id = ""
                        time.sleep(10)
                        continue
                    else:
                        log(self.index, f"⚠️ Gagal daftar: {reg.text[:100]}")
                        time.sleep(10)
                else:
                    log(self.index, f"⚠️ Gagal daftar: HTTP {reg.status_code}. Mencoba recover ID...")
                    recovered_id = self.recover_agent_id()
                    if recovered_id:
                        self.agent_id = str(recovered_id)
                        log(self.index, f"✅ RECOVER BERHASIL (Dari HTTP {reg.status_code})! ID: {self.agent_id}")
                        return
                    time.sleep(15)
                    
            except requests.exceptions.Timeout:
                log(self.index, "⏱️ Timeout, coba lagi...")
                time.sleep(5)
            except Exception as e:
                log(self.index, f"❌ Error join: {e}")
                time.sleep(10)

    # ==================== STATE MANAGEMENT ====================
    def get_state_with_backoff(self, retries=3):
        now = time.time()
        elapsed = now - self.last_state_time
        if elapsed < self.min_state_interval:
            time.sleep(self.min_state_interval - elapsed)
        
        max_retries = retries
        base_delay = 1
        
        for attempt in range(max_retries):
            try:
                # Delay tambahan dihapus karena rate limit sudah 50/detik
                res = requests.get(
                    f"{BASE_URL}/games/{self.game_id}/agents/{self.agent_id}/state",
                    headers=self.headers,
                    timeout=10
                )
                
                self.last_state_time = time.time()
                
                if res.status_code == 200:
                    self.consecutive_errors = 0
                    return res
                    
                elif res.status_code == 429:
                    retry_after = int(res.headers.get("Retry-After", base_delay * (2 ** attempt)))
                    log(self.index, f"⚠️ Rate limit (429), tunggu {retry_after}s")
                    time.sleep(retry_after)
                    continue
                    
                elif res.status_code == 404:
                    # HTTP 404: Game Room sudah dihapus atau Agent ID sudah hangus (Game Selesai)
                    # Solusi: Stop retries percuma, paksa bot membersihkan ID agar mencari game baru
                    log(self.index, f"♻️ Game tidak ditemukan (404). Kemungkinan game telah berakhir. Mereset ke lobi awal...")
                    self.agent_id = ""
                    self.game_id = ""
                    return None
                    
                else:
                    log(self.index, f"⚠️ State error {res.status_code}, attempt {attempt+1}")
                    time.sleep(base_delay * (attempt + 1))
                    
            except requests.exceptions.Timeout:
                log(self.index, f"⏱️ State timeout, attempt {attempt+1}")
                time.sleep(base_delay * (attempt + 1))
            except Exception as e:
                log(self.index, f"❌ State exception: {e}")
                time.sleep(base_delay * (attempt + 1))
        
        self.consecutive_errors += 1
        return None

    # ==================== ACTION SENDER ====================
    # Group 2 actions (no cooldown, no thought needed)
    GROUP2_ACTIONS = {"pickup", "equip", "talk", "whisper", "broadcast"}
    
    def send_action(self, action_obj, thought=""):
        max_retries = 4

        for attempt in range(max_retries):
            try:
                # Jangan kirim thought agar tidak muncul "thought revealed" di game log.
                payload = {"action": action_obj}

                res = requests.post(
                    f"{BASE_URL}/games/{self.game_id}/agents/{self.agent_id}/action",
                    json=payload,
                    headers=self.headers,
                    timeout=25
                )

                if res.status_code in (200, 201, 202):
                    try:
                        return res.json()
                    except:
                        return {"success": True, "message": "Accepted/OK"}

                elif res.status_code == 429:
                    retry_after = int(res.headers.get("Retry-After", 3))
                    log(self.index, f"⚠️ Action rate limit, tunggu {retry_after}s")
                    time.sleep(retry_after)
                    continue
                else:
                    err_data = {}
                    try:
                        err_data = res.json()
                    except:
                        pass

                    code = err_data.get("error", {}).get("code")
                    if code == "ALREADY_ACTED":
                        return {"success": False, "code": "ALREADY_ACTED", "message": "Too early"}
                    if code == "COOLDOWN_ACTIVE":
                        return {"success": False, "code": "COOLDOWN_ACTIVE", "message": "Group 1 cooldown active"}
                    if code == "INSUFFICIENT_EP":
                        return {"success": False, "code": "INSUFFICIENT_EP", "message": "Not enough EP"}

                    log(self.index, f"⚠️ Action error {res.status_code}: {res.text[:50]}")
                    return {"success": False, "code": code or f"HTTP_{res.status_code}", "message": f"HTTP {res.status_code}"}

            except Exception as e:
                log(self.index, f"❌ Send action error: {e}")
                if attempt == max_retries - 1:
                    return None
                time.sleep(2)

        return None
    # ==================== PENGETAHUAN MAP ====================
    def update_map_knowledge(self, state):
        curr = state.get("currentRegion")
        if curr:
            self.known_regions[curr["id"]] = curr
            if curr["id"] != self.current_region_id:
                # Pindah region
                self.current_region_id = curr["id"]
                self.turns_in_region = 1
                if curr["id"] not in self.visited_regions:
                    self.visited_regions.add(curr["id"])
                    self.region_first_visit[curr["id"]] = time.time()
                    log(self.index, f"📍 Region baru: {curr.get('name', curr['id'][:8])}")
                else:
                    log(self.index, f"📍 Kembali ke: {curr.get('name', curr['id'][:8])}")
            else:
                self.turns_in_region += 1

        for reg in state.get("visibleRegions", []):
            if "id" in reg:
                # Update/Merge dengan memori lama
                old = self.known_regions.get(reg["id"], {})
                old.update(reg)
                self.known_regions[reg["id"]] = old
            
        for conn in state.get("connectedRegions", []):
            if isinstance(conn, dict) and conn.get("id"):
                c_id = str(conn["id"])
                old = dict(self.known_regions.get(c_id, {}))  # type: ignore
                old.update(conn)
                self.known_regions[c_id] = old  # type: ignore

        pending = state.get("pendingDeathzones", [])
        self.pending_deathzones = {str(dz["id"]) for dz in pending if isinstance(dz, dict) and "id" in dz}  # type: ignore
        if self.pending_deathzones and self.current_region_id in self.pending_deathzones:  # type: ignore
            log(self.index, "⚠️ PERINGATAN: Region ini akan jadi deathzone!")  # type: ignore

    def is_region_safe(self, region_id):
        if region_id in self.pending_deathzones:
            return False
        reg = self.known_regions.get(region_id)
        if reg and reg.get("isDeathZone"):
            return False
        return True

    def find_escape_route(self, connections, avoid_region_id=None, current_region_id=None):
        # Normalisasi connections (bisa string bisa dict)
        conn_ids = []
        for c in connections:
            cid = c["id"] if isinstance(c, dict) else c
            # Filter SUPER KETAT: Dilarang kembali ke tempat kita berpijak sekarang!
            if cid != current_region_id:
                conn_ids.append(cid)

        unvisited_safe = [r for r in conn_ids if r not in self.visited_regions and self.is_region_safe(r)]
        if unvisited_safe:
            return random.choice(unvisited_safe)
        
        safe = [r for r in conn_ids if self.is_region_safe(r)]
        if len(safe) > 1 and avoid_region_id in safe:
            safe.remove(avoid_region_id)
            
        if safe:
            return random.choice(safe)
        
        # Prioritas 3: Terkepung. Pilih mana saja yang belum meledak (meskipun pending)
        # Tapi tetap hindari yang aktif meledak (isDeathZone == True)
        not_active_death = []
        for r in conn_ids:
            reg = self.known_regions.get(r) # Get the region object, could be None
            if reg is not None and not reg.get("isDeathZone"): # Add guard for None and check isDeathZone
                not_active_death.append(r)
                
        if len(not_active_death) > 1 and avoid_region_id in not_active_death:
            not_active_death.remove(avoid_region_id)
            
        if not_active_death:
            return random.choice(not_active_death)
        
        # Prioritas 4: Keputusan terakhir. Jika SEMUA tempat aktif deathzone,
        # Berarti kita benar-benar terkepung. Lebih baik diam saja untuk hemat EP / serang musuh.
        return None

    def find_best_retreat_route(self, state):
        me = state.get("self", {})
        connections = state.get("currentRegion", {}).get("connections", []) or []
        conn_ids = [(c.get("id") if isinstance(c, dict) else c) for c in connections]
        conn_ids = [str(cid) for cid in conn_ids if cid]
        moltz_count = sum(
            1 for item in (me.get("inventory") or [])
            if item.get("category") == "currency" or "moltz" in str(item.get("name", "")).lower()
        )

        best_region = None
        best_score = -999
        allies = self.get_squad_allies() if self.squad_mode_active(state) else []
        ally_regions = {str(ally.get("region_id", "")) for ally in allies}

        for region_id in conn_ids:
            if not self.is_region_safe(region_id):
                continue

            enemy_pressure = self.region_enemy_pressure(region_id, state)
            score = 50 - enemy_pressure * 22

            if region_id not in self.visited_regions:
                score += 10
            if region_id in ally_regions and enemy_pressure <= 1:
                score += 10

            reg = self.known_regions.get(region_id, {})
            terrain = reg.get("terrain", "")
            if terrain == "hills":
                score += 8
            if terrain == "water":
                score -= 18

            if self.is_late_game() or moltz_count >= 25:
                score -= enemy_pressure * 12

            if score > best_score:
                best_region = region_id
                best_score = score

        return best_region

    # ==================== INVENTORY MANAGEMENT ====================
    def evaluate_weapon(self, item):
        if not item: return 0
        name = item.get("name", "Fist")
        stats = WEAPONS.get(name, {"damage": 10, "range": 0, "value": 0})
        return stats["value"]

    def has_usable_weapon(self, me):
        equipped = me.get("equippedWeapon")
        if not equipped or equipped.get("name") == "Fist":
            return False
        return True

    def get_weapon_damage(self, me):
        equipped = me.get("equippedWeapon")
        if not equipped or equipped.get("name") == "Fist":
            return 5, 0  # Damage kecil, range 0
        
        weapon_name = equipped.get("name")
        stats = WEAPONS.get(weapon_name, WEAPONS["Fist"])
        return me["atk"] + stats["atkBonus"], stats["range"]

    def manage_inventory(self, state):
        actions = []
        me = state.get("self") or {}
        inventory = me.get("inventory") or []
        equipped = me.get("equippedWeapon")
        current_region = me.get("regionId")
        ground_items = [ei.get("item", {}) for ei in state.get("visibleItems", []) if ei.get("regionId") == current_region]
        
        # --- ANALISIS ISI TAS SEKARANG (Karena TIDAK BISA DROP, harus pilih-pilih) ---
        has_sniper = any(i["name"] in ["Sniper", "Sniper Rifle"] for i in inventory)
        has_katana = any(i["name"] == "Katana" for i in inventory)
        has_sidearm = any(i["name"] in ["Bow", "Pistol"] for i in inventory)
        has_binoc = any(i["name"] == "Binoculars" for i in inventory)
        has_energy_drink = any(i["name"] == "Energy Drink" for i in inventory)
        has_any_weapon = any(i.get("category") == "weapon" for i in inventory)
        core_ready = has_sniper and has_katana and has_sidearm and has_binoc and has_energy_drink
        max_recovery_keep = 6 if core_ready else 3
        recovery_count = 0
        
        for i in inventory:
            cat = i.get("category")
            if cat == "weapon":
                continue
            elif cat == "recovery":
                recovery_count = int(recovery_count) + 1  # type: ignore

        # 1. SMART PICKUP (Hanya ambil jika slot kategori masih kosong)
        if len(inventory) < 10:
            for item in ground_items:
                if not item:
                    continue
                if len(inventory) + len(actions) >= 10:
                    break # Stop if inventory is full or will be full after pending actions
                
                name = item.get("name", "")
                cat = item.get("category")
                should_pick = False
                
                is_moltz = "moltz" in name.lower() or cat in ("money", "currency")
                if is_moltz:
                    should_pick = True
                elif name in ["Sniper", "Sniper Rifle"] and not has_sniper: 
                    should_pick = True
                    has_sniper = True
                    has_any_weapon = True
                elif name == "Katana" and not has_katana: 
                    should_pick = True
                    has_katana = True
                    has_any_weapon = True
                elif name in ["Bow", "Pistol"] and not has_sidearm:
                    should_pick = True
                    has_sidearm = True
                    has_any_weapon = True
                elif name == "Binoculars" and not has_binoc: 
                    should_pick = True
                    has_binoc = True
                elif name == "Energy Drink" and not has_energy_drink:
                    should_pick = True
                    has_energy_drink = True
                elif cat == "weapon":
                    name_check = "Sniper" if name == "Sniper Rifle" else name
                    if not has_any_weapon and name_check in ["Sword", "Knife"]:
                        # Fallback: jangan terlalu lama bertarung pakai Fist
                        should_pick = True
                        has_any_weapon = True
                elif cat == "recovery" and name != "Energy Drink" and int(recovery_count) < max_recovery_keep:  # type: ignore
                    should_pick = True
                    recovery_count = int(recovery_count) + 1  # type: ignore
                
                if should_pick:
                    actions.append({"type": "pickup", "itemId": item["id"]})
                    log(self.index, f"📂 Smart Pickup: {name}")

        # 2. EQUIP SENJATA TERBAIK (Cek dari inventory + baru dipungut)
        # Gabungkan inventory lama dengan item yang baru mau dipungut
        picked_up_items = []
        for act in actions:
            if act["type"] == "pickup":
                # Cari detail item dari ground_items
                found = next((gi for gi in ground_items if gi["id"] == act["itemId"]), None)
                if found:
                    picked_up_items.append(found)

        full_inventory_potential = list(inventory)
        full_inventory_potential.extend(picked_up_items)
        
        best_weapon = None
        weapons_in_bag = [i for i in full_inventory_potential if i.get("category") == "weapon"]
        if weapons_in_bag:
            def weapon_score(w):
                name = w["name"]
                if name in ["Sniper", "Sniper Rifle"]:
                    return 1000
                if name == "Katana":
                    return 800
                
                lookup_name = "Sniper" if name == "Sniper Rifle" else name
                stats = WEAPONS.get(lookup_name, {"damage": 0, "range": 0})
                return stats["damage"] * (stats["range"] + 1)
            
            best_weapon = max(weapons_in_bag, key=weapon_score)
            
        if best_weapon:
            # Jika best_weapon ini adalah item yang sudah dipakai, abaikan
            if not equipped or not isinstance(equipped, dict) or equipped.get("id") != best_weapon.get("id"):
                actions.append({"type": "equip", "itemId": best_weapon["id"]})
                log(self.index, f"🔧 Langsung Equip: {best_weapon['name']}")
                
        return actions

    # ==================== KEPUTUSAN TEMPUR ====================
    def detect_under_attack(self, current_hp, current_region, visible_agents):
        if self.last_hp is not None and isinstance(self.last_hp, (int, float)) and current_hp < self.last_hp:
            damage = self.last_hp - current_hp
            # Ambil semua kemungkinan attacker (tidak hanya di region saat ini)
            enemies_around = [a for a in visible_agents if a["id"] != self.agent_id]
            if enemies_around:
                log(self.index, f"💥 DISERANG! Kehilangan {damage} HP")
                return True, enemies_around
        return False, []

    def hits_to_kill(self, target, damage):
        if damage < DAMAGE_PER_HIT_MIN:
            return 999
        
        if "def" in target:
            damage_per_hit = max(1, damage - (target["def"] * 0.5))
        else:
            damage_per_hit = damage
        
        if damage_per_hit < DAMAGE_PER_HIT_MIN:
            return 999
        
        return (target["hp"] + damage_per_hit - 1) // damage_per_hit

    def is_worth_attacking(self, target, damage, target_type, state, weapon_range, dist):
        hits = self.hits_to_kill(target, damage)
        me = state["self"]
        ep = me.get("ep", 0)
        
        # Agent: Melee (Range 0) vs Range
        if target_type == "agent":
            if dist == 0:
                # Jarak Dekat Murni (Melee/0 Range)
                # Maksimal hit 3 kali mati (Menghindari kerugian di jarak dekat)
                return hits <= 3, hits
            else:
                # Sniping (Jarak Jauh 1-2 Range)
                # Sniper butuh 4-5 hit untuk membunuh musuh full HP. Kita izinkan menembak (hits <= 5).
                # Tapi jika butuh lebih dari 2 hit, pastikan EP mencukupi.
                if hits > 2 and ep < 4:
                    return False, hits
                return hits <= 5, hits
        
        # Monster: Cukup 1-2 Turn
        if target_type == "monster":
            return hits <= 2, hits
        
        return False, hits

    def find_best_target(self, state, damage, weapon_range):
        me = state["self"]
        curr_region = state["currentRegion"]
        connections = curr_region.get("connections", [])
        visible_agents = state.get("visibleAgents", [])
        visible_monsters = state.get("visibleMonsters", [])
        squad_allies = self.get_squad_allies() if self.squad_mode_active(state) else []
        ally_regions = {str(ally.get("region_id", "")) for ally in squad_allies}
        
        best_target = None
        best_score = 999
        best_type = None
        best_hits = 999

        focus_target = self.get_focus_target(state)
        if focus_target and focus_target.get("id") != self.agent_id:
            focus_region = focus_target.get("regionId")
            is_here = focus_region == me["regionId"]
            is_adj = any((isinstance(c, dict) and c.get("id") == focus_region) or c == focus_region for c in connections)
            focus_dist = 0 if is_here else (1 if is_adj else 2)
            if focus_dist <= weapon_range:
                worth, hits = self.is_worth_attacking(focus_target, damage, "agent", state, weapon_range, focus_dist)
                engage, _ = self.should_engage(state, focus_target, focus_dist)
                if worth and engage:
                    return focus_target, "agent", hits
        
        # JIKA SOLO: Inisiasi Insting Berburu
        # Kita izinkan mengevaluasi musuh selama tubuh kita tidak sekarat banget (< 40)
        can_initiate_attack = me["hp"] >= 40
        
        # 1. EVALUASI AGENT (Prioritas Utama)
        for a in visible_agents:
            if a.get("id") == self.agent_id or not a.get("isAlive", True):  # type: ignore
                continue
            
            # Jika darah kita kritis (< 40), dan ini bukan self-defense (di-passing dari decide_action), 
            # abaikan target hunting mencari amannya dulu.
            if not can_initiate_attack:
                continue
            
            ag_hp = a.get("hp", 100)
            
            # PAWN TIDAK DIBATASI LAST HIT LAGI. 
            # Jika ketemu lawan, hajar saja bareng-bareng! Team Kill lebih penting.
            
            # Cek Jarak Sebenarnya (Terutama untuk perhitungan Sniper Range +2 yang tepat)
            is_here = a["regionId"] == me["regionId"]
            is_adj = any((isinstance(c, dict) and c.get("id") == a["regionId"]) or c == a["regionId"] for c in connections)  # type: ignore
            
            # Default asumsinya dia jauh (range 2), tapi akan dikoreksi
            dist = 2
            if is_here:
                dist = 0
            elif is_adj:
                dist = 1
                
            # Jika dia bukan di sini (dist 0), dan bukan di samping (dist 1), 
            # tapi statusnya 'visibleAgents', maka secara mekanik game dia di dist 2 atau lebih (biasanya 2 karena vision).
            if dist > weapon_range:
                continue

            # Hitung TTK (Time to Kill)
            worth, hits = self.is_worth_attacking(a, damage, "agent", state, weapon_range, dist)  # type: ignore
            if not worth:
                continue
            engage, risk = self.should_engage(state, a, dist)
            if not engage and a.get("hp", 100) > 25:
                continue
            
            
            # --- APEX SCORING ---
            # Skor lebih kecil = Lebih Prioritas
            score = hits * 10 + risk
            
            # Bonus: Target Sekarat (Executioner Mode)
            if a["hp"] <= 25:
                score -= 30 
            
            # Bonus: Keuntungan Jarak (Sniper aman dari Melee)
            eq_enemy = a.get("equippedWeapon") or {}
            enemy_range = WEAPONS.get(eq_enemy.get("name", "Fist"), {"range": 0})["range"]
            if dist > enemy_range:
                score -= 15
            
            # Bonus Terrain: Sniper di Hills sangat mematikan
            if dist > 0 and curr_region.get("terrain") == "hills":
                score -= 10

            # Bonus kerja sama: target yang berada di sekitar ally lebih berharga untuk difokus.
            if str(a.get("regionId", "")) in ally_regions:
                score -= 18

            if enemy_range == 0 and weapon_range > 0 and dist > 0:
                score -= 12
            if a.get("hp", 100) <= 40 and dist > 0:
                score -= 8

            if score < best_score:
                best_score, best_target, best_type, best_hits = score, a, "agent", hits
        
        # 2. EVALUASI MONSTER (Hanya jika tidak ada Agent yang menarik)
        if not best_target or best_score > 50: # Jika best_score agent masih tinggi (misal > 50), pertimbangkan monster
            for m in visible_monsters:
                is_here = m["regionId"] == me["regionId"]
                is_adj = any((isinstance(c, dict) and c.get("id") == m["regionId"]) or c == m["regionId"] for c in connections)
                
                dist_m = 2
                if is_here:
                    dist_m = 0
                elif is_adj:
                    dist_m = 1
                
                if dist_m > weapon_range:
                    continue
                    
                worth, hits = self.is_worth_attacking(m, damage, "monster", state, weapon_range, dist_m)
                if worth:
                    score = hits * 20 # Monster kurang prioritas dibanding agent
                    if score < best_score:
                        best_score, best_target, best_type, best_hits = score, m, "monster", hits
        
        return best_target, best_type, best_hits

    def decide_action(self, state, under_attack=False, attackers=[]):
        me = state["self"]
        curr = state["currentRegion"]
        hp = me["hp"]
        ep = me["ep"]
        inventory = me.get("inventory", [])
        moltz_count = sum(1 for item in inventory if item.get("category") == "currency" or "moltz" in str(item.get("name", "")).lower())
        connections = curr.get("connections", [])
        
        # Update pengetahuan map
        self.update_map_knowledge(state)
        
        # Reset tracker explore jika pindah region
        if self.last_region_id != me["regionId"]:
            self.last_region_id = me["regionId"]
            self.explore_count_in_region = 0
            self.turns_in_region = 0
            
        # Deteksi serangan
        under_attack, attackers = self.detect_under_attack(hp, me["regionId"], state.get("visibleAgents", []))
        
        # Cek weapon
        has_weapon = self.has_usable_weapon(me)
        damage, weapon_range = self.get_weapon_damage(me)
        
        # ===== PRIORITAS 1: DEATH ZONE (AKTIF ATAU PENDING) =====
        is_dangerous = curr.get("isDeathZone") or curr["id"] in self.pending_deathzones
        if is_dangerous:
            escape = self.find_escape_route(connections, self.last_region_id, curr["id"])
            if escape:
                msg = "🚨 DARURAT: Keluar death zone aktif!" if curr.get("isDeathZone") else "⚠️ PERINGATAN: Zona akan meledak, pindah!"
                log(self.index, msg)
                return {"type": "move", "regionId": escape}, "Escape danger zone"
            else:
                log(self.index, "🚧 TERKEPUNG DEATHZONE! Tidak ada jalan keluar aman. Bersiap tempur di tempat!")
                # Kita tidak me-return apapun di sini, agar bot lanjut mengeksekusi logika 
                # Attack musuh / Heal / dsb di sisa kode di bawah ini.

        late_game_survival = self.is_late_game() and (hp < 85 or moltz_count >= 15 or me.get("kills", 0) >= 2)
        current_enemy_pressure = self.region_enemy_pressure(me["regionId"], state)
        if late_game_survival and current_enemy_pressure >= 2:
            retreat = self.find_best_retreat_route(state)
            if retreat and retreat != me["regionId"]:
                log(self.index, f"🛡️ LATE GAME SURVIVAL: keluar dari cluster musuh ({current_enemy_pressure})")
                return {"type": "move", "regionId": retreat}, "Late game retreat"
        
        # ===== PRIORITAS 2: PENGEROYOKAN (ESCAPE FIRST) =====
        if under_attack:
            # Cari musuh spesifik di dalam jarak tembak kita (weapon_range)
            valid_targets = []
            for a in attackers:
                # Perkiraan jarak: Sama = 0, Tetangga = 1, Jauh = 2+
                is_here = (a["regionId"] == me["regionId"])
                is_adj = any((isinstance(c, dict) and c["id"] == a["regionId"]) or c == a["regionId"] for c in connections)
                dist = 0 if is_here else (1 if is_adj else 9)
                
                if dist <= weapon_range:
                    valid_targets.append(a)

            # Jika ada musuh yang BISA kita jangkau dengan senjata kita
            if valid_targets:
                valid_targets.sort(key=lambda x: x.get("hp", 100))
                target_agent = valid_targets[0]
                
                # Strategi 1: MENGHINDARI THIRD PARTY (CROSSFIRE)
                # Jika kita sadar ada 2 atau lebih pemain lain di area ini dan kita sedang diserang,
                # itu adalah situasi berisiko tinggi dikeroyok. Mundur taktis ("Bukan pengecut, tapi jenius").
                if len(attackers) >= 2 and hp < 85:
                    escape = self.find_best_retreat_route(state) or self.find_escape_route(connections, self.last_region_id, curr["id"])
                    if escape:
                        log(self.index, f"🏃 TERJEBAK CROSSFIRE ({len(attackers)} MUSUH)! Mundur taktis menghindari third-party...")
                        return {"type": "move", "regionId": escape}, "Retreat from crossfire"
                
                # Kalah senjata jarak dekat (Kabur individual)
                if not has_weapon and hp < 50:
                    escape = self.find_best_retreat_route(state) or self.find_escape_route(connections, self.last_region_id, curr["id"])
                    if escape:
                        log(self.index, "🏃 HP Menipis & kalah gear dari penyerang, mundur mencari celah!")
                        return {"type": "move", "regionId": escape}, "Retreat low hp"

            else:
                # KITA DISERANG, TAPI MUSUH DI LUAR JANGKAUAN (Contoh: Di-snipe, kita pegang Katana)
                # JANGAN buang-buang turn buat serang! Kabur ke tempat aman.
                escape = self.find_best_retreat_route(state) or self.find_escape_route(connections, self.last_region_id, curr["id"])
                if escape:
                    log(self.index, f"🎯 Tembakan Sniper dari jauh! Mundur mencari perlindungan!")
                    return {"type": "move", "regionId": escape}, "Retreat from sniper"

        # ===== PRIORITAS 2.5: BELA DIRI (Setelah pasti aman dari keroyokan) =====
        if under_attack and valid_targets:
            # Karena sudah lolos dari kondisi lari keroyokan di atas, kita berani membalas!
            target_agent = valid_targets[0]
            
            # Perkirakan jarak lagi untuk target_agent ini
            dist_ta = 0 if target_agent["regionId"] == me["regionId"] else 1
            _, hits = self.is_worth_attacking(target_agent, damage, "agent", state, weapon_range, dist_ta)
            
            # SOLO MODE REVISION: Kalau kita dikeroyok / atau sendirian, pertimbangkan HP.
            # Meskipun 1 vs 1, kalau HP kita < 35, lebih baik jangan asal Attack balik, kabur!
            if hp < 35 and target_agent["hp"] > 25:
                escape = self.find_best_retreat_route(state) or self.find_escape_route(connections, self.last_region_id, curr["id"])
                if escape:
                    log(self.index, "🏃 HP Kritis di Bawah 35! Daripada mati bela diri, mending lari cari obat!")
                    return {"type": "move", "regionId": escape}, "Retreat to heal self"
            
            if has_weapon or target_agent["hp"] < 30:
                if hits <= 1:
                    self.target_loot_region = target_agent["regionId"]
                log(self.index, f"⚔️ BELA DIRI: Menghajar balik {target_agent['name']} (Hits: {hits})")
                return {"type": "attack", "targetId": target_agent["id"], "targetType": "agent"}, "Self defense"

        # ===== PRIORITAS 3: HEAL & RECOVERY UTAMA =====
        # 1. Gunakan Medkit/Bandage jika HP kurang
        if hp < HP_LOW:
            recovery = [i for i in inventory if i.get("category") == "recovery"]
            if recovery:
                best = max(recovery, key=lambda i: RECOVERY_ITEMS.get(i["name"], {}).get("priority", 0))
                heal_amount = RECOVERY_ITEMS.get(best["name"], {}).get("hpRestore", 0)
                
                if heal_amount > 0 and (100 - hp) >= (heal_amount * 0.4): # Izinkan heal meski belum terlalu rugi
                    log(self.index, f"💚 Tindakan Medis: Memakai {best['name']} (HP Saat Ini: {hp})")
                    return {"type": "use_item", "itemId": best["id"]}, f"Heal {best['name']}"
                elif hp < 50 and heal_amount > 0:
                    # Darurat: paksa heal pakai apa saja walau rugi / overheal
                    log(self.index, f"💚 Tindakan Medis DARURAT: Memakai {best['name']} (HP {hp})")
                    return {"type": "use_item", "itemId": best["id"]}, f"Heal Darurat"

        # 2. Gunakan Energy Drink jika EP sangat rendah
        if ep < 3:
            e_drinks = [i for i in inventory if i.get("name") == "Energy Drink"]
            if e_drinks:
                log(self.index, f"⚡ Minum Energy Drink untuk menumpuk stamina (EP: {ep})")
                return {"type": "use_item", "itemId": e_drinks[0]["id"]}, "Use Energy Drink"

        # ===== PRIORITAS 3.5: KABUR JIKA KRITIS DEMI MENCARI PERLINDUNGAN =====
        if hp < HP_CRITICAL and not under_attack:
            # Jika EP kita cukup, coba pindah menjauhi keramaian
            if ep >= 3:
                escape = self.find_best_retreat_route(state) or self.find_escape_route(connections, self.last_region_id, curr["id"])
                if escape and escape != curr["id"]:
                    log(self.index, f"🏃 HP Menipis tanpa medkit, cari tempat sembunyi yang aman...")
                    return {"type": "move", "regionId": escape}, "Safe retreat (No Heal)"
            else:
                log(self.index, f"😰 HP Kritis tapi EP habis, terpaksa berdiam / cari sisa reruntuhan...")

        # ===== PRIORITAS 4: PENGHEMATAN EP (STORM) & REST =====
        weather = curr.get("weather", "clear")
        is_storm = (weather == "storm")
        
        # Logika EP DiSederhanakan:
        # Menyerang Butuh 2 EP. Move butuh 1 EP (2 di storm).
        # Jadi klo EP sisa 0 atau 1 (apalagi storm), tidur saja untuk tabung stamina!
        if ep < 2 or (is_storm and ep < 3):
            log(self.index, f"😴 Rest / menabung tenaga (Darah aman, EP: {ep}, Weather: {weather})")
            return {"type": "rest"}, "Recover EP"

        # ===== PRIORITAS 4.5: AMBIL LOOT HASIL SNIPE =====
        if self.target_loot_region and not under_attack:
            # Karena sniper bisa range 2, target mungkin bukan di connections.
            # Jadi kita move ke arah target tersebut selangkah demi selangkah.
            
            target_r = self.target_loot_region
            self.target_loot_region = None


            
            # CEK INTELIJEN: Cek keamanan wilayah. Apakah jadi ajang jebakan/kumpul massa?
            region_is_safe = self.is_region_safe(target_r)
            
            visible_enemies = [ag for ag in state.get("visibleAgents", []) if ag["id"] != self.agent_id]
            enemies_exactly_there = any(ag["regionId"] == target_r for ag in visible_enemies)
            
            # Jika ada 3 musuh atau lebih di layar radar saat kita mau me-loot, 
            # itu artinya wilayah tersebut sedang sangat kacau parah. MENDING JANGAN KE SANA!
            is_crossfire_zone = len(visible_enemies) >= 3
            
            is_adjacent = any((isinstance(c, dict) and c.get("id") == target_r) or c == target_r for c in connections)
            
            if not region_is_safe or enemies_exactly_there or is_crossfire_zone or self.should_avoid_region(target_r, state, hp, moltz_count):
                log(self.index, "🛑 Batal pungut Moltz korban! Radar mendeteksi bahaya kerumunan / sarang jebakan.")
            else:
                next_step = target_r
                if not is_adjacent:
                    # Target mati di range 2 (gak adjacent). Coba cari grid connection yang terhubung ke sana
                    found_path = False
                    for c in connections:
                        c_id = c["id"] if isinstance(c, dict) else c
                        reg_c = self.known_regions.get(c_id, {})
                        c_conns = reg_c.get("connections", [])
                        if any((isinstance(nc, dict) and nc.get("id") == target_r) or nc == target_r for nc in c_conns):
                            next_step = c_id
                            found_path = True
                            break
                    
                    if not found_path:
                        log(self.index, "🛑 Batal pungut Moltz! Rute titik kematian terlalu gelap/sulit dicapai (Blind Drop).")
                    else:
                        log(self.index, f"🎯 Otw {next_step} untuk ambil loot Moltz si korban!")
                        return {"type": "move", "regionId": next_step}, "Looting after snipe"
                else:
                    log(self.index, f"🎯 Otw {next_step} untuk ambil loot Moltz si korban!")
                    return {"type": "move", "regionId": next_step}, "Looting after snipe"

        
        # ===== PRIORITAS 5: INSTING BERBURU (AGRESI PENUH PERHITUNGAN) =====
        target, target_type, hits = self.find_best_target(state, damage, weapon_range)
        if target:
            ag_hp = target.get("hp", 100)
            is_here = target.get("regionId") == me.get("regionId")
            is_adj = any((isinstance(c, dict) and c.get("id") == target.get("regionId")) or c == target.get("regionId") for c in connections)
            dist_target = 0 if is_here else (1 if is_adj else 2)
            engage, risk = self.should_engage(state, target, dist_target)
            
            # Kapan kita BERBURU untuk Kill Ranking?
            # 1. Musuh sedang lemah (HP di bawah 45) -> Kita jdi Algojo!
            # 2. ATAU Kita sudah memegang senjata kuat (Katana, Sniper, Bow, Pistol, Sword) DAN Darah kita sangat Prima (>= 70)
            # 3. ATAU Musuh bisa mati dalam 1-2 tembakan apapun senjatanya
            is_weak_target = ag_hp < 45
            
            equipped_weapon = me.get("equippedWeapon") or {}
            equipped_weapon_name = equipped_weapon.get("name", "")
            
            is_deadly_weapon = equipped_weapon_name in ["Sniper", "Katana", "Sword", "Pistol", "Bow"]
            is_prime_condition = hp >= 70
            easy_kill = hits <= 2
            last_hit_window = ag_hp <= max(18, damage)
            
            if engage and (is_weak_target or (is_deadly_weapon and is_prime_condition) or easy_kill or last_hit_window):
                behavior = "SNIPE" if target["regionId"] != me["regionId"] else "ASSASSINATE"
                log_msg = f"🎯 INSTING BERBURU: {behavior} {target_type} {target.get('name', 'Unknown')} (HP: {ag_hp}, Hits: {hits}, Risk: {risk})"
                log(self.index, log_msg)
                
                if hits <= 1:
                    self.target_loot_region = target["regionId"]
                self.remember_focus_target(target)
                    
                return {
                    "type": "attack", 
                    "targetId": target["id"], 
                    "targetType": target_type
                }, f"Hunting {behavior} ({hits} hit)"

        # ===== PRIORITAS 5.5: SUPPORT ALLY & AMBIL LOOT TERLIHAT =====
        if not under_attack:
            support_route = self.choose_support_route(state)
            if support_route and hp >= 65:
                target_region, score = support_route
                log(self.index, f"🤝 Support ally ke {target_region} (score {score})")
                return {"type": "move", "regionId": target_region}, "Support ally"

            loot_route = self.choose_visible_loot_route(state)
            if loot_route:
                target_region, score = loot_route
                log(self.index, f"🧭 Rotasi ke loot terlihat {target_region} (score {score})")
                return {"type": "move", "regionId": target_region}, "Move toward visible loot"
        
        # ===== PRIORITAS 6: CARI WEAPON =====
        if not has_weapon:
            # Cek weapon di ground
            for entry in state.get("visibleItems", []):
                if entry["regionId"] == me["regionId"] and entry["item"].get("category") == "weapon":
                    # Akan di-pickup oleh free action, untuk main action lebih baik explore
                    log(self.index, "🔍 Ada weapon di ground, akan diambil")
                    break
            # Cek supply cache
            interactables = curr.get("interactables", [])
            supply_cache = next((f for f in interactables if f["type"] == "supply_cache" and not f.get("isUsed")), None)
            if supply_cache:
                cache_name = supply_cache.get("name", "Supply Cache")
                log(self.index, f"📦 Interact {cache_name} (Weapon/Moltz)")
                return {"type": "interact", "interactableId": supply_cache["id"]}, "Interact cache"
        
        # ===== PRIORITAS 7: INTERACT FACILITY (Hanya jika aman!) =====
        if not under_attack:
            interactables = curr.get("interactables", [])
            useful = [f for f in interactables if not f.get("isUsed") and f["type"] != "cave"]
            if useful:
                def fac_score(f):
                    # Mode SOLO: Semua bot bebas mengambil hadiah dan Moltz (Supply Cache)
                    if f["type"] == "supply_cache":
                        return 5
                    if f["type"] == "medical_facility" and hp < 80:
                        return 4
                    if f["type"] == "watchtower":
                        return 2
                    return 0
                
                best_fac = max(useful, key=fac_score)
                if fac_score(best_fac) > 0:
                    fac_name = best_fac.get("name", best_fac["type"])
                    log(self.index, f"🏛️ Interact: {fac_name}")
                    return {"type": "interact", "interactableId": best_fac["id"]}, f"Use {best_fac['type']}"
        
        # ===== PRIORITAS 8: MOVE (JELAJAH) =====
        # Jika sedang storm, hindari move kecuali terpaksa (EP habis 2)
        if is_storm and ep < 6 and not is_dangerous:
            log(self.index, "⛈️ Storm: Menetap untuk hemat EP")
            return {"type": "rest"}, "Storm EP Save"

        conn_ids = []
        for c in connections:
            cid = c["id"] if isinstance(c, dict) else c
            conn_ids.append(cid)

        # Cari region yang belum dikunjungi
        unvisited = [rid for rid in conn_ids if rid not in self.visited_regions and self.is_region_safe(rid)]
        target = self.choose_low_risk_region(unvisited, state, hp, moltz_count)
        if target:
            log(self.index, f"🗺️ Move ke region baru")
            return {"type": "move", "regionId": target}, "Explore new region"
        
        # Atau pindah ke mana saja yang aman
        safe = [rid for rid in conn_ids if self.is_region_safe(rid)]
        target = self.choose_low_risk_region(safe, state, hp, moltz_count)
        if target:
            log(self.index, f"🚶 Move ke region lain")
            return {"type": "move", "regionId": target}, "Move to another region"
        
        # Jika benar-benar terjepit, baru Istirahat
        log(self.index, "😴 Rest (Tidak ada pergerakan aman)")
        return {"type": "rest"}, "No safe moves"

    def run(self):
        log(self.index, f"🚀 Bot {self.name} started")
        self.find_and_join_game()
        
        while True:
            try:
                # Jika ID hangus karena game over / server reset 404, cari room baru!
                if not self.agent_id or not self.game_id:
                    self.find_and_join_game()
                    self.consecutive_errors = 0
                    continue
                
                # 1. SELALU ambil state terbaru untuk Free Actions (Looting)
                state_res = self.get_state_with_backoff()
                if not state_res:
                    self.consecutive_errors += 1
                    time.sleep(5)
                    continue
                
                res_json = state_res.json()
                data = res_json.get("data")
                if not data:
                    log(self.index, "⚠️ Server return status OK tapi data KOSONG. Skip.")
                    time.sleep(2)
                    continue
                if not data.get("self"):
                    log(self.index, "⚠️ Data 'self' hilang. Mencoba ulang...")
                    continue

                me = data["self"]
                current_status = data.get("gameStatus")
                
                self.consecutive_errors = 0 # Reset error counter
                
                # ===== CEK PERUBAHAN STATUS =====
                if self.last_game_status != current_status:
                    log(self.index, f"📊 Status game: {current_status}")
                    self.last_game_status = current_status
                
                # ===== CEK STATUS GAME =====
                # Hitung Moltz di tas saat ini
                inventory = data.get("self", {}).get("inventory", [])
                self.current_game_moltz = sum(1 for item in inventory if item.get("category") == "currency" or item.get("name") == "Moltz")
                self.update_squad_sync(data)

                if not me["isAlive"]:
                    self.total_games += 1
                    self.total_losses += 1
                    self.total_moltz_earned += self.current_game_moltz
                    log(self.index, f"💀 Agent mati. Kills: {me.get('kills', 0)}")
                    log(self.index, f"📊 [STATISTIK] 👑 Menang: {self.total_wins} | 💀 Kalah: {self.total_losses} | 💰 Total Moltz Dikumpulkan: {self.total_moltz_earned} | Koin Match ini: {self.current_game_moltz}")
                    structured_log(self.index, "MATCH_RESULT", result="loss", game_id=self.game_id, kills=me.get("kills", 0), moltz=self.current_game_moltz, total_wins=self.total_wins, total_losses=self.total_losses, hp=me.get("hp", 0))
                    time.sleep(5)
                    self.last_game_id = self.game_id
                    self.agent_id = ""
                    self.find_and_join_game()
                    continue
                
                if current_status == "finished":
                    self.total_games += 1
                    # Jika game finished dan kita masih isAlive, kita anggap Menang (Rank tinggi)
                    self.total_wins += 1 
                    self.total_moltz_earned += self.current_game_moltz
                    log(self.index, f"🏆 Game selesai dan kamu bertahan hidup! Kills: {me.get('kills', 0)}")
                    log(self.index, f"📊 [STATISTIK] 👑 Menang: {self.total_wins} | 💀 Kalah: {self.total_losses} | 💰 Total Moltz Dikumpulkan: {self.total_moltz_earned} | Koin Match ini: {self.current_game_moltz}")
                    structured_log(self.index, "MATCH_RESULT", result="win", game_id=self.game_id, kills=me.get("kills", 0), moltz=self.current_game_moltz, total_wins=self.total_wins, total_losses=self.total_losses, hp=me.get("hp", 0))
                    time.sleep(5)
                    self.last_game_id = self.game_id
                    self.agent_id = ""
                    self.find_and_join_game()
                    continue
                
                if current_status == "waiting":
                    log(self.index, f"⏳ Game waiting... next check in {self.waiting_poll_interval}s")
                    time.sleep(self.waiting_poll_interval)
                    self.waiting_poll_interval = min(self.waiting_poll_interval + 2, self.max_waiting_interval)
                    continue
                
                # ===== GAME RUNNING =====
                if current_status == "running":
                    self.waiting_poll_interval = 5
                    
                    if getattr(self, "game_start_time", 0.0) == 0.0:
                        self.game_start_time = time.time()
                        log(self.index, "🎮 GAME DIMULAI!")
                        structured_log(self.index, "MATCH_START", game_id=self.game_id, agent_id=self.agent_id, agent_name=self.name)
                    
                    # Cek serangan secara real-time
                    under_attack = False
                    if self.last_hp is not None and me["hp"] < self.last_hp:
                        under_attack = True
                        damage_taken = self.last_hp - me["hp"]
                        log(self.index, f"💥 DISERANG! Kehilangan {damage_taken} HP (Sisa: {me['hp']})")
                    
                    # Update HP tracker untuk perbandingan turn depan
                    self.last_hp = me["hp"]
                    
                    # ===== FREE ACTIONS (Group 2 - No thought) =====
                    free_actions = self.manage_inventory(data)
                    for action_fa in free_actions:
                        self.send_action(action_fa)  # Tanpa thought agar tidak spam
                        time.sleep(self.free_action_spacing)
                    
                    # ===== MAIN ACTION =====
                    all_enemies = data.get("visibleAgents", []) + data.get("visibleMonsters", [])
                    action_result = self.decide_action(data, under_attack, all_enemies)
                    
                    action = action_result[0] if isinstance(action_result, tuple) else {"type": "rest"}
                    reason = action_result[1] if isinstance(action_result, tuple) else "Fallback"

                    if isinstance(action, dict) and action.get("type") == "move" and not self.is_region_safe(action.get("regionId", "")):
                        log(self.index, "🛑 Wilayah bahaya, batalkan move.")
                        continue

                    # 4. KIRIM AKSI UTAMA & CATAT WAKTU (TURBO START)
                    turn_start_time = time.time()
                    result = self.send_action(action, reason)
                    
                    if result and (result.get("success") or result.get("accepted")):
                        log(self.index, f"✅ [{action['type']}] Accepted.")
                        
                        # --- POST-ACTION LOOTING (1x scan saja, anti-spam) ---
                        if action["type"] in ["move", "explore", "attack"]:
                            time.sleep(1.0)
                            post_state_res = self.get_state_with_backoff(retries=1)
                            if post_state_res:
                                post_data = post_state_res.json().get("data")
                                if post_data:
                                    pfa_list = self.manage_inventory(post_data)
                                    for pfa in pfa_list:
                                        self.send_action(pfa)  # Tanpa thought
                                        time.sleep(self.free_action_spacing)

                        # --- JEDA TURN (PANGGIL HANDLE TUNGGU) ---
                        self.handle_turn_wait(turn_start_time, action["type"])
                    else:
                        err_code = ""
                        if isinstance(result, dict):
                            err_code = str(result.get("code") or result.get("error", {}).get("code") or "")
                        err_msg = result.get("message", "Unknown err") if isinstance(result, dict) else "Timeout/Rate Limit (No response dari server)"
                        log(self.index, f"⚠️ Aksi ditolak: {err_msg}")
                        structured_log(self.index, "BOT_ERROR", game_id=self.game_id, action=action.get("type", "unknown") if isinstance(action, dict) else "unknown", code=err_code or "UNKNOWN", message=err_msg)
                        if err_code in ("ALREADY_ACTED", "COOLDOWN_ACTIVE", "INSUFFICIENT_EP"):
                            # Jika cooldown/EP belum cukup, tunggu sampai turn berikutnya agar sinkron lagi
                            self.handle_turn_wait(turn_start_time, action["type"])
                        else:
                            time.sleep(2)
                
            except Exception as e:
                log(self.index, f"❌ Loop error: {e}")
                structured_log(self.index, "BOT_ERROR", game_id=self.game_id or "-", action="loop", code="EXCEPTION", message=str(e))
                time.sleep(10)

    def handle_turn_wait(self, turn_start, last_action_type):
        """Handle sinkronisasi turn dan tunggu cooldown usai"""
        now = time.time()
        elapsed = now - turn_start
        # Tambah jitter kecil per bot supaya 10 bot tidak request serempak
        target_turn = 61.2 + self.turn_jitter
        remaining_wait = target_turn - elapsed

        if remaining_wait > 0:
            log(self.index, f"⏲️ Turn sukses! Looting selesai, sisa tunggu: {remaining_wait:.1f}s (Total Sync: {target_turn:.1f}s)")
            time.sleep(remaining_wait)

        return None, None

# Trik type hinting untuk pylint/pyre
hasattr_dict = hasattr({}, "get")

# ==================== MAIN ====================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Gunakan: python script.py [nomor_bot]")
        print("Contoh: python script.py 1")
        sys.exit(1)
    
    bot_num = int(sys.argv[1])
    if bot_num < 1 or bot_num > 20:
        print("Nomor bot harus 1-20")
        sys.exit(1)
    
    bot = EliteAgent(bot_num)
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[{bot_num}] Bot terhenti karena error tidak terduga: {e}")




























