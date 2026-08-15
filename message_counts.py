#!/usr/bin/env python3
"""Count Discord messages per author over a date range and emit TSV.

Usage:
    python message_counts.py                        # last two weeks
    python message_counts.py 2026-07-01 2026-07-31
    python message_counts.py 2026-07-01 2026-07-31 --tz America/New_York -o july.tsv

Reads DISCORD_BOT_TOKEN from .env. Both dates are inclusive whole days.
Requires the Message Content privileged intent to report message text.
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from dotenv import load_dotenv

DEFAULT_GUILD = "3 Putt for Life"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export per-author Discord message counts as TSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("start", nargs="?",
                   help="Start date, YYYY-MM-DD (inclusive). "
                        "Defaults to 13 days before the end date.")
    p.add_argument("end", nargs="?",
                   help="End date, YYYY-MM-DD (inclusive). Defaults to today.")
    p.add_argument("-g", "--guild", default=DEFAULT_GUILD,
                   help="Server name or ID")
    p.add_argument("--tz", default="UTC",
                   help="Timezone the dates are expressed in, e.g. America/New_York")
    p.add_argument("-o", "--output", help="Also write the TSV to this file")
    p.add_argument("--exclude-bots", action="store_true",
                   help="Omit bot accounts from the results")
    p.add_argument("--include-threads", dest="threads", action="store_true",
                   default=True, help="Include thread messages (default)")
    p.add_argument("--no-threads", dest="threads", action="store_false",
                   help="Skip threads, count only top-level channel messages")
    return p.parse_args()


def date_bounds(start: Optional[str], end: Optional[str], tzname: str
                ) -> Tuple[datetime, datetime]:
    """Return UTC datetimes spanning [start 00:00, end 24:00) in the given tz.

    Omitted dates default to the last two weeks ending today.
    """
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        sys.exit(f"Unknown timezone: {tzname!r}")
    try:
        end_d = (datetime.strptime(end, "%Y-%m-%d").date() if end
                 else datetime.now(tz).date())
        start_d = (datetime.strptime(start, "%Y-%m-%d").date() if start
                   else end_d - timedelta(days=13))
    except ValueError:
        sys.exit("Dates must be YYYY-MM-DD")
    if end_d < start_d:
        sys.exit("End date is before start date")
    after = datetime.combine(start_d, time.min, tz)
    before = datetime.combine(end_d + timedelta(days=1), time.min, tz)
    return after.astimezone(timezone.utc), before.astimezone(timezone.utc)


def find_guild(client: discord.Client, wanted: str) -> discord.Guild:
    if wanted.isdigit():
        guild = client.get_guild(int(wanted))
        if guild:
            return guild
    lowered = wanted.lower()
    for guild in client.guilds:
        if guild.name.lower() == lowered:
            return guild
    available = ", ".join(f"{g.name} ({g.id})" for g in client.guilds) or "none"
    sys.exit(f"Server {wanted!r} not found. Bot is in: {available}")


async def collect_channels(guild: discord.Guild, include_threads: bool
                           ) -> List[discord.abc.Messageable]:
    """Every channel and thread whose history the bot may read."""
    targets: List[discord.abc.Messageable] = []
    me = guild.me

    parents = list(guild.text_channels) + list(guild.forums)
    for parent in parents:
        perms = parent.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            print(f"  skipping #{parent.name} (no read access)", file=sys.stderr)
            continue
        # Forum channels hold no messages themselves, only threads.
        if isinstance(parent, discord.TextChannel):
            targets.append(parent)
        if not include_threads:
            continue
        targets.extend(parent.threads)
        try:
            async for thread in parent.archived_threads(limit=None):
                targets.append(thread)
            if perms.manage_threads:
                async for thread in parent.archived_threads(limit=None, private=True):
                    targets.append(thread)
        except discord.HTTPException as exc:
            print(f"  archived threads in #{parent.name}: {exc}", file=sys.stderr)

    # Threads can appear in both .threads and the archived listing.
    seen = set()
    unique = []
    for target in targets:
        if target.id not in seen:
            seen.add(target.id)
            unique.append(target)
    return unique


async def tally(guild: discord.Guild, after: datetime, before: datetime,
                include_threads: bool
                ) -> Tuple[Counter, Dict[int, discord.abc.User], int]:
    counts: Counter = Counter()
    authors: Dict[int, discord.abc.User] = {}
    total = 0

    channels = await collect_channels(guild, include_threads)
    print(f"Scanning {len(channels)} channels/threads in {guild.name}...",
          file=sys.stderr)

    for channel in channels:
        try:
            async for message in channel.history(limit=None, after=after,
                                                 before=before, oldest_first=True):
                counts[message.author.id] += 1
                authors.setdefault(message.author.id, message.author)
                total += 1
        except discord.Forbidden:
            print(f"  skipping {channel.name} (forbidden)", file=sys.stderr)
        except discord.HTTPException as exc:
            print(f"  error reading {channel.name}: {exc}", file=sys.stderr)
    return counts, authors, total


def build_tsv(counts: Counter, authors: Dict[int, discord.abc.User],
              exclude_bots: bool) -> str:
    rows = ["Display Name\tUsername\tUser ID\tMessages"]
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(authors[kv[0]])))
    for user_id, count in ranked:
        author = authors[user_id]
        if exclude_bots and author.bot:
            continue
        display = getattr(author, "display_name", author.name)
        rows.append(f"{display}\t{author.name}\t{user_id}\t{count}")
    return "\n".join(rows) + "\n"


async def run(args: argparse.Namespace, token: str) -> None:
    after, before = date_bounds(args.start, args.end, args.tz)

    intents = discord.Intents.default()  # message content is not needed to count
    client = discord.Client(intents=intents)
    failure: Optional[BaseException] = None

    @client.event
    async def on_ready() -> None:
        nonlocal failure
        try:
            guild = find_guild(client, args.guild)
            counts, authors, total = await tally(guild, after, before, args.threads)
            tsv = build_tsv(counts, authors, args.exclude_bots)
            print(f"{total} messages from {len(counts)} authors "
                  f"({after.isoformat()} to {before.isoformat()})", file=sys.stderr)
            sys.stdout.write(tsv)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as fh:
                    fh.write(tsv)
                print(f"Wrote {args.output}", file=sys.stderr)
        except BaseException as exc:  # includes SystemExit from find_guild
            failure = exc
        finally:
            await client.close()

    await client.start(token)
    if failure is not None:
        raise failure


def main() -> None:
    args = parse_args()
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set in .env")
    try:
        asyncio.run(run(args, token))
    except discord.LoginFailure:
        sys.exit("Discord rejected the bot token")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
