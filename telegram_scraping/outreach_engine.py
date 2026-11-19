"""
Telegram Group Member Extractor & Personalized Outreach Engine
Part of Lead Hunter Suite.
Extracts members from Telegram groups/channels and generates/sends
personalized inquiry messages (e.g. Bitrix24 CRM, accounting, logistics).

Includes safety features:
- Dry-run preview queue before sending.
- Anti-spam throttles and jitter delays.
- Privacy error and flood wait handling.
- Dual CSV export (, and ;) for queue inspection and approval.
"""

import os
import re
import sys
import time
import shutil
import random
import asyncio
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User, Channel, Chat
from telethon.errors import FloodWaitError, UserPrivacyRestrictedError, PeerFloodError

from common.utils import clean_cell, save_csv_dual

VERSION = "1.3.0"

# Common fallback paths for Telegram credentials & session
POSSIBLE_ENV_PATHS = [
    os.path.join(os.getcwd(), ".env"),
    "/mnt/d/CRM consulting/otabek/.env",
]

POSSIBLE_SESSION_PATHS = [
    os.path.join(os.getcwd(), "telegram_session.session"),
    "/mnt/d/CRM consulting/otabek/telegram_session.session",
]


class TelegramOutreachEngine:
    """Finds group members and manages personalized messaging campaigns."""

    def __init__(self, env_path: Optional[str] = None, session_path: Optional[str] = None):
        self.env_path = env_path or self._find_first_existing(POSSIBLE_ENV_PATHS)
        self.session_source = session_path or self._find_first_existing(POSSIBLE_SESSION_PATHS)
        self.temp_session_path = None
        self.client = None

        if self.env_path:
            load_dotenv(self.env_path)

        self.api_id = os.environ.get("TELEGRAM_API_ID") or os.environ.get("TG_API_ID")
        self.api_hash = os.environ.get("TELEGRAM_API_HASH") or os.environ.get("TG_API_HASH")

    @staticmethod
    def _find_first_existing(paths: List[str]) -> Optional[str]:
        for p in paths:
            if os.path.exists(p):
                return p
        return None

    def _prepare_session(self) -> str:
        """
        Creates a temporary isolated copy of the session file to avoid
        'sqlite3.OperationalError: database is locked' if an MCP server or another
        client is running concurrently.
        """
        if not self.session_source or not os.path.exists(self.session_source):
            raise FileNotFoundError(
                f"Telegram session not found. Checked locations: {POSSIBLE_SESSION_PATHS}"
            )

        temp_dir = os.path.join(os.getcwd(), ".cache")
        os.makedirs(temp_dir, exist_ok=True)
        temp_base = os.path.join(temp_dir, f"outreach_worker_{os.getpid()}")
        temp_file = temp_base + ".session"

        shutil.copy2(self.session_source, temp_file)
        self.temp_session_path = temp_base
        return temp_base

    async def connect(self):
        """Initializes and connects Telethon client."""
        if not self.api_id or not self.api_hash:
            raise ValueError(
                "Telegram API_ID and API_HASH not found in environment or .env."
            )

        session_base = self._prepare_session()
        self.client = TelegramClient(session_base, int(self.api_id), self.api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise PermissionError("Telegram session is not authorized. Please log in first.")

        me = await self.client.get_me()
        print(f"[Outreach Engine v{VERSION}] Connected as: {me.first_name} (@{me.username or 'NoUsername'})")
        return me

    async def disconnect(self):
        """Disconnects client and cleans up temporary session file."""
        if self.client:
            await self.client.disconnect()
            self.client = None

        if self.temp_session_path:
            for ext in [".session", ".session-journal"]:
                f = self.temp_session_path + ext
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    async def extract_group_members(
        self, chat_id_or_username, limit: int = 100, include_history_speakers: bool = True
    ) -> Tuple[str, List[Dict]]:
        """
        Extracts member list from a group/chat.
        If member list is restricted or for extra context, mines recent message history
        to find active speakers and their message topics.
        """
        await self.connect()
        me = await self.client.get_me()
        my_id = me.id

        # Resolve entity
        target = chat_id_or_username
        if isinstance(target, str) and (target.startswith("-") or target.isdigit()):
            target = int(target)

        entity = await self.client.get_entity(target)
        group_title = getattr(entity, "title", str(chat_id_or_username))
        print(f"[Outreach Engine] Target group identified: '{group_title}'")

        members_dict = {}

        # 1. Direct group participants (works on open supergroups/groups)
        try:
            async for p in self.client.iter_participants(entity, limit=limit):
                if p.bot or p.id == my_id:
                    continue
                first_name = p.first_name or ""
                last_name = p.last_name or ""
                full_name = f"{first_name} {last_name}".strip() or "Уважаемый клиент"
                members_dict[p.id] = {
                    "user_id": str(p.id),
                    "full_name": full_name,
                    "first_name": first_name or "Коллега",
                    "username": f"@{p.username}" if p.username else "",
                    "phone": p.phone or "",
                    "role_context": "Участник группы",
                    "source_group": group_title,
                }
            print(f"[Outreach Engine] Extracted {len(members_dict)} direct participants.")
        except Exception as e:
            print(f"[Outreach Engine] Direct participant list unavailable ({e}). Mining message history...")

        # 2. Mine active message history for recent speakers and conversation context
        if include_history_speakers:
            try:
                messages = await self.client.get_messages(entity, limit=min(limit * 2, 200))
                for m in messages:
                    if not m.sender or not isinstance(m.sender, User) or m.sender.bot or m.sender.id == my_id:
                        continue
                    u = m.sender
                    snippet = (m.text or "")[:80].replace("\n", " ").strip()
                    if u.id not in members_dict:
                        first_name = u.first_name or ""
                        last_name = u.last_name or ""
                        full_name = f"{first_name} {last_name}".strip() or "Уважаемый коллега"
                        members_dict[u.id] = {
                            "user_id": str(u.id),
                            "full_name": full_name,
                            "first_name": first_name or "Коллега",
                            "username": f"@{u.username}" if u.username else "",
                            "phone": u.phone or "",
                            "role_context": f"Активный автор: {snippet}" if snippet else "Активный участник",
                            "source_group": group_title,
                        }
                    elif snippet and members_dict[u.id]["role_context"] == "Участник группы":
                        members_dict[u.id]["role_context"] = f"Последняя тема: {snippet}"
            except Exception as e:
                print(f"[Outreach Engine] Message history scan notice: {e}")

        await self.disconnect()
        return group_title, list(members_dict.values())

    @staticmethod
    def generate_personalized_message(member: Dict, campaign_type: str = "bitrix_crm") -> str:
        """
        Generates a natural, professional, personalized cold/warm outreach message
        tailored to the target member and niche.
        """
        first_name = member.get("first_name", "коллега")
        group_title = member.get("source_group", "деловой группе")

        if campaign_type == "bitrix_crm":
            # Natural B2B Bitrix24 inquiry
            return (
                f"Здравствуйте, {first_name}! "
                f"Обращаюсь к вам из группы «{group_title}». "
                f"Подскажите, пожалуйста, актуальна ли сейчас для вас или вашей компании оптимизация "
                f"Битрикс24 CRM (интеграция с WhatsApp/Wazzup, сквозная аналитика, связка с МойСклад "
                f"или аудит действующих бизнес-процессов)? "
                f"Будем рады проконсультировать и поделиться кейсами внедрения."
            )

        elif campaign_type == "accounting":
            # Outsource accounting inquiry for procurement/B2B suppliers
            return (
                f"Здравствуйте, {first_name}! "
                f"Вижу вашу активность в сфере поставок и закупок. "
                f"Подскажите, рассматривает ли ваша компания передачу бухгалтерского и налогового "
                f"учёта на надёжный аутсорсинг (работа с казначейством, тендерными счетами, "
                f"НДС и отчётностью для поставщиков)? "
                f"Можем предложить бесплатный первичный аудит."
            )

        elif campaign_type == "china_logistics":
            # China-to-Kazakhstan logistics
            return (
                f"Здравствуйте, {first_name}! "
                f"Подскажите, заказывает ли ваша компания товары или сырье напрямую из Китая? "
                f"Предоставляем прямую логистику под ключ (Китай — Казахстан): быстрый поиск фабрик, "
                f"выкуп, таможенное оформление и белая доставка до склада."
            )

        else:
            return (
                f"Здравствуйте, {first_name}! "
                f"Пишу вам по поводу сотрудничества в сфере автоматизации бизнеса и внедрения CRM. "
                f"Будет ли вам интересно обсудить возможные точки роста?"
            )

    def prepare_campaign_queue(
        self, members: List[Dict], campaign_type: str = "bitrix_crm", prefix: Optional[str] = None
    ) -> List[Dict]:
        """Prepares outreach queue with generated texts and pending status."""
        queue = []
        for m in members:
            msg = self.generate_personalized_message(m, campaign_type=campaign_type)
            if prefix:
                msg = f"{prefix.strip()}\n\n{msg}"
            queue.append({
                "user_id": m["user_id"],
                "full_name": m["full_name"],
                "first_name": m["first_name"],
                "username": m["username"],
                "phone": m.get("phone", ""),
                "source_group": m.get("source_group", ""),
                "campaign_type": campaign_type,
                "personalized_message": msg,
                "status": "READY_FOR_REVIEW",
                "sent_at": "",
                "notes": m.get("role_context", ""),
            })
        return queue

    async def execute_dispatch(
        self,
        queue: List[Dict],
        live: bool = False,
        max_messages: int = 5,
        min_delay: int = 25,
        max_delay: int = 60,
    ) -> List[Dict]:
        """
        Executes outreach.
        If live=False, performs a safe DRY-RUN (prints previews, updates nothing).
        If live=True, connects to Telegram and delivers messages with rate limit intervals.
        """
        print(f"\n=======================================================")
        print(f"=== Telegram Outreach Dispatcher (Lead Hunter v{VERSION}) ===")
        print(f"=== Mode: {'LIVE (REAL SENDING)' if live else 'DRY RUN (PREVIEW ONLY)'} ===")
        print(f"=== Total Queue Size: {len(queue)} | Max per batch: {max_messages} ===")
        print(f"=======================================================\n")

        if not live:
            print("[DRY RUN] No messages will be sent to real accounts.")
            for i, item in enumerate(queue[:max_messages], 1):
                recipient = item["username"] or item["full_name"]
                print(f"\n[{i}/{min(len(queue), max_messages)}] Target: {recipient} (ID: {item['user_id']})")
                print(f"Text Preview:\n\"\"\"\n{item['personalized_message']}\n\"\"\"")
                item["status"] = "DRY_RUN_PASSED"
            return queue

        # LIVE MODE
        await self.connect()
        sent_count = 0

        for i, item in enumerate(queue):
            if sent_count >= max_messages:
                print(f"[Dispatcher] Reached batch limit of {max_messages} messages. Pausing safely.")
                break

            target_id = int(item["user_id"])
            recipient_label = item["username"] or item["full_name"]
            message_text = item["personalized_message"]

            print(f"[{sent_count + 1}/{max_messages}] Sending to {recipient_label} (ID: {target_id})...")

            try:
                target = item["username"] if item.get("username") else target_id
                entity = await self.client.get_entity(target)
                await self.client.send_message(entity, message_text)
                item["status"] = "SENT"
                item["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                sent_count += 1
                print(f" -> SUCCESS: Sent to {recipient_label}")

                # Rate limiting delay
                if sent_count < max_messages:
                    sleep_time = random.randint(min_delay, max_delay)
                    print(f" -> Sleeping for {sleep_time}s to maintain safe anti-spam reputation...")
                    await asyncio.sleep(sleep_time)

            except UserPrivacyRestrictedError:
                print(f" -> SKIPPED: {recipient_label} restricted messages from non-contacts.")
                item["status"] = "PRIVACY_RESTRICTED"
            except FloodWaitError as e:
                print(f" -> TELEGRAM FLOOD WAIT: Must wait {e.seconds}s. Aborting live batch.")
                item["status"] = f"FLOOD_WAIT_{e.seconds}s"
                break
            except PeerFloodError:
                print(f" -> PEER FLOOD: Account rate limited by Telegram. Stopping dispatch.")
                item["status"] = "PEER_FLOOD_STOPPED"
                break
            except Exception as e:
                print(f" -> ERROR sending to {recipient_label}: {e}")
                item["status"] = f"ERROR: {str(e)[:50]}"

        await self.disconnect()
        return queue


def run_telegram_outreach(
    group_id_or_username,
    campaign_type: str = "bitrix_crm",
    limit: int = 50,
    live: bool = False,
    prefix: Optional[str] = None,
    output_csv: str = "data/telegram_outreach_queue.csv",
):
    """Convenience synchronous entrypoint."""
    engine = TelegramOutreachEngine()
    
    # 1. Extract
    group_title, members = asyncio.run(
        engine.extract_group_members(group_id_or_username, limit=limit)
    )
    
    # 2. Prepare queue
    queue = engine.prepare_campaign_queue(members, campaign_type=campaign_type, prefix=prefix)
    
    # 3. Dispatch (Dry Run or Live)
    updated_queue = asyncio.run(
        engine.execute_dispatch(
            queue,
            live=live,
            max_messages=min(len(queue), 5),
            min_delay=15,
            max_delay=30,
        )
    )
    
    # 4. Save dual CSV for human review
    fieldnames = [
        "user_id",
        "full_name",
        "first_name",
        "username",
        "phone",
        "source_group",
        "campaign_type",
        "personalized_message",
        "status",
        "sent_at",
        "notes",
    ]
    save_csv_dual(output_csv, fieldnames, updated_queue)
    print(f"\n[Outreach Engine] Queue and campaign logs saved to:")
    print(f"  - Comma format:     {output_csv}")
    print(f"  - Semicolon format: {output_csv.replace('.csv', '_semicolon.csv')}")
    return updated_queue
