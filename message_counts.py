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
    p.add_argument("--exclude-bots", dest="exclude_bots", action="store_true",
                   default=True, help="Omit bot accounts (default)")
    p.add_argument("--include-bots", dest="exclude_bots", action="store_false",
                   help="Include bot accounts in the results")
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
            # Only text channels have private archived threads.
            if perms.manage_threads and isinstance(parent, discord.TextChannel):
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


def message_text(message: discord.Message) -> str:
    """One-line, tab-safe rendering of a message for a TSV cell."""
    text = " ".join(message.content.split())
    if not text:
        if message.attachments:
            text = "[{}]".format(", ".join(a.filename for a in message.attachments))
        elif message.stickers:
            text = "[sticker: {}]".format(message.stickers[0].name)
        elif message.embeds:
            text = "[embed]"
        else:
            text = "[no text]"
    return text


async def tally(guild: discord.Guild, after: datetime, before: datetime,
                include_threads: bool
                ) -> Tuple[Counter, Dict[int, discord.abc.User],
                           Dict[int, Tuple[datetime, str]], int]:
    counts: Counter = Counter()
    authors: Dict[int, discord.abc.User] = {}
    last: Dict[int, Tuple[datetime, str]] = {}
    total = 0

    channels = await collect_channels(guild, include_threads)
    print(f"Scanning {len(channels)} channels/threads in {guild.name}...",
          file=sys.stderr)

    for channel in channels:
        try:
            async for message in channel.history(limit=None, after=after,
                                                 before=before, oldest_first=True):
                author_id = message.author.id
                counts[author_id] += 1
                authors[author_id] = message.author
                total += 1
                # Channels are scanned one at a time, so compare across them.
                if author_id not in last or message.created_at > last[author_id][0]:
                    last[author_id] = (message.created_at, message_text(message))
        except discord.Forbidden:
            print(f"  skipping {channel.name} (forbidden)", file=sys.stderr)
        except discord.HTTPException as exc:
            print(f"  error reading {channel.name}: {exc}", file=sys.stderr)
    return counts, authors, last, total


async def resolve_names(guild: discord.Guild,
                        authors: Dict[int, discord.abc.User]) -> Dict[int, str]:
    """Map author IDs to server nicknames.

    Message history hands back a bare User for anyone not in the member cache,
    whose display_name misses the per-guild nickname. Look each one up against
    the guild, falling back to whatever the message carried for people who have
    since left.
    """
    names: Dict[int, str] = {}
    for user_id, author in authors.items():
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None  # left the server
            except discord.HTTPException as exc:
                print(f"  could not resolve {author.name}: {exc}", file=sys.stderr)
                member = None
        source = member or author
        names[user_id] = getattr(source, "display_name", None) or source.name
    return names


def build_tsv(counts: Counter, names: Dict[int, str],
              last: Dict[int, Tuple[datetime, str]], tzname: str) -> str:
    tz = ZoneInfo(tzname)
    rows = ["Name\tMessages\tLast Date\tLast Message"]
    for user_id, count in counts.items():
        display = names[user_id]
        posted_at, text = last[user_id]
        stamp = posted_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{display}\t{count}\t{stamp}\t{text}")
    header, body = rows[0], rows[1:]
    body.sort(key=lambda row: row.split("\t")[0].casefold())
    return "\n".join([header] + body) + "\n"


async def run(args: argparse.Namespace, token: str) -> None:
    after, before = date_bounds(args.start, args.end, args.tz)

    intents = discord.Intents.default()
    intents.message_content = True  # required to read the last message's text
    client = discord.Client(intents=intents)
    failure: Optional[BaseException] = None

    @client.event
    async def on_ready() -> None:
        nonlocal failure
        try:
            guild = find_guild(client, args.guild)
            counts, authors, last, total = await tally(guild, after, before,
                                                       args.threads)
            if args.exclude_bots:
                bots = [uid for uid in counts if authors[uid].bot]
                for user_id in bots:
                    total -= counts.pop(user_id)
                if bots:
                    print(f"Excluded {len(bots)} bot account(s)", file=sys.stderr)
            names = await resolve_names(guild, authors)
            tsv = build_tsv(counts, names, last, args.tz)
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
    except discord.PrivilegedIntentsRequired:
        sys.exit("Enable the Message Content intent for this bot at "
                 "https://discord.com/developers/applications -> Bot -> "
                 "Privileged Gateway Intents")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
