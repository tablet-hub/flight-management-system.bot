"""
Flight announcement & booking Discord bot (discord.py 2.4+)

Features
  - Staff announce flights with a "Book Ticket" button
  - Members book / view history / cancel tickets privately (ephemeral replies)
  - Bot DMs booked members when check-in opens
  - Seat tracking, auto "departed" status, flight cancellation with DM notices
"""

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("MTU2NzkwNDg4ODQ0NzMxMTk1Ng.GNUdoc.E_EZFPIbioSrzqDwtmC1Vn5M-PDcMH5SBxpDg4")
GUILD_ID = int(os.getenv("1552773061894348952") or 0) or None
STAFF_ROLE_ID = int(os.getenv("1554508013967114342") or 0) or None
ANNOUNCE_CHANNEL_ID = int(os.getenv("1554496632890986498") or 0) or None
PING_ROLE_ID = int(os.getenv("1554508013967114342") or 0) or None
LOG_CHANNEL_ID = int(os.getenv("1554496632890986498") or 0) or None
DB_PATH = os.getenv("DB_PATH", "flights.db")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flightbot")

GREEN, GREY, RED, BLUE = 0x2ECC71, 0x95A5A6, 0xE74C3C, 0x3498DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    aircraft      TEXT,
    price         INTEGER NOT NULL DEFAULT 0,
    seats         INTEGER NOT NULL,
    departure_ts  INTEGER NOT NULL,
    checkin_ts    INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | departed | cancelled
    guild_id      INTEGER,
    channel_id    INTEGER,
    message_id    INTEGER,
    host_id       INTEGER
);
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ref           TEXT UNIQUE NOT NULL,
    flight_id     INTEGER NOT NULL REFERENCES flights(id),
    user_id       INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',      -- active | cancelled | flight_cancelled
    booked_ts     INTEGER NOT NULL,
    cancelled_ts  INTEGER,
    notified      INTEGER NOT NULL DEFAULT 0
);
-- A member can only hold one active ticket per flight (also blocks race conditions)
CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket
    ON bookings(flight_id, user_id) WHERE status = 'active';
"""

db: aiosqlite.Connection = None  # set in setup_hook
db_lock = asyncio.Lock()          # serialises booking / cancelling so seats never oversell


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
async def get_flight(flight_id: int):
    cur = await db.execute("SELECT * FROM flights WHERE id = ?", (flight_id,))
    return await cur.fetchone()


async def booked_count(flight_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM bookings WHERE flight_id = ? AND status = 'active'", (flight_id,)
    )
    return (await cur.fetchone())[0]


def new_ref() -> str:
    return "TK-" + secrets.token_hex(3).upper()


def parse_departure(text: str) -> Optional[int]:
    """Expects 'YYYY-MM-DD HH:MM' in UTC."""
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #
def flight_embed(f, booked: int) -> discord.Embed:
    left = max(f["seats"] - booked, 0)
    if f["status"] == "cancelled":
        color, state, prefix = RED, "❌ Cancelled", "CANCELLED · "
    elif f["status"] == "departed":
        color, state, prefix = GREY, "🛫 Departed", ""
    elif left == 0:
        color, state, prefix = RED, "🔴 Fully booked", ""
    else:
        color, state, prefix = GREEN, "🟢 Open for booking", ""

    e = discord.Embed(
        title=f"✈️ {prefix}Flight {f['code']}: {f['origin']} → {f['destination']}",
        color=color,
    )
    e.add_field(name="Departure", value=f"<t:{f['departure_ts']}:F>\n<t:{f['departure_ts']}:R>", inline=True)
    e.add_field(name="Check-in opens", value=f"<t:{f['checkin_ts']}:t>\n<t:{f['checkin_ts']}:R>", inline=True)
    e.add_field(name="Status", value=state, inline=True)
    e.add_field(name="Seats", value=f"{left}/{f['seats']} available", inline=True)
    if f["aircraft"]:
        e.add_field(name="Aircraft", value=f["aircraft"], inline=True)
    if f["price"]:
        e.add_field(name="Price", value=f"{f['price']:,}", inline=True)
    if f["host_id"]:
        e.add_field(name="Host", value=f"<@{f['host_id']}>", inline=True)
    e.set_footer(text=f"Flight ID {f['id']} · Times shown in your local timezone")
    return e


def ticket_embed(ref: str, f, title: str = "🎟️ Ticket confirmed") -> discord.Embed:
    e = discord.Embed(title=title, color=BLUE)
    e.add_field(name="Ticket", value=f"`{ref}`", inline=True)
    e.add_field(name="Flight", value=f"{f['code']}", inline=True)
    e.add_field(name="Route", value=f"{f['origin']} → {f['destination']}", inline=True)
    e.add_field(name="Departure", value=f"<t:{f['departure_ts']}:F>", inline=True)
    e.add_field(name="Check-in opens", value=f"<t:{f['checkin_ts']}:F>", inline=True)
    return e


def jump_url(f) -> Optional[str]:
    if f["guild_id"] and f["channel_id"] and f["message_id"]:
        return f"https://discord.com/channels/{f['guild_id']}/{f['channel_id']}/{f['message_id']}"
    return None


# --------------------------------------------------------------------------- #
# Announcement message + persistent Book button
# --------------------------------------------------------------------------- #
class BookButton(discord.ui.DynamicItem[discord.ui.Button], template=r"book:(?P<flight_id>\d+)"):
    """Persistent button: keeps working after bot restarts."""

    def __init__(self, flight_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Book Ticket",
                emoji="🎟️",
                style=discord.ButtonStyle.success,
                custom_id=f"book:{flight_id}",
                disabled=disabled,
            )
        )
        self.flight_id = flight_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["flight_id"]))

    async def callback(self, interaction: discord.Interaction):
        await book_ticket(interaction, self.flight_id)


def make_view(flight_id: int, disabled: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BookButton(flight_id, disabled))
    return view


async def refresh_announcement(flight_id: int):
    """Re-render the public announcement (seat count, status, button state)."""
    f = await get_flight(flight_id)
    if not f or not f["message_id"]:
        return
    try:
        channel = bot.get_channel(f["channel_id"]) or await bot.fetch_channel(f["channel_id"])
        msg = await channel.fetch_message(f["message_id"])
        booked = await booked_count(flight_id)
        disabled = f["status"] != "scheduled" or booked >= f["seats"]
        await msg.edit(embed=flight_embed(f, booked), view=make_view(flight_id, disabled))
    except discord.HTTPException as exc:
        log.warning("Could not refresh announcement for flight %s: %s", flight_id, exc)


async def dm_user(user_id: int, embed: discord.Embed) -> bool:
    try:
        user = await bot.fetch_user(user_id)
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return False


async def log_to_staff(text: str):
    log.info(text)
    if LOG_CHANNEL_ID:
        try:
            ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Booking / cancelling logic
# --------------------------------------------------------------------------- #
async def book_ticket(interaction: discord.Interaction, flight_id: int):
    await interaction.response.defer(ephemeral=True)  # reply is only visible to the member

    async with db_lock:
        f = await get_flight(flight_id)
        if not f:
            return await interaction.followup.send("❌ That flight no longer exists.", ephemeral=True)
        if f["status"] != "scheduled" or f["departure_ts"] <= now():
            return await interaction.followup.send("❌ Booking for this flight is closed.", ephemeral=True)
        if await booked_count(flight_id) >= f["seats"]:
            return await interaction.followup.send("❌ Sorry, this flight is fully booked.", ephemeral=True)

        ref = new_ref()
        try:
            await db.execute(
                "INSERT INTO bookings (ref, flight_id, user_id, booked_ts) VALUES (?, ?, ?, ?)",
                (ref, flight_id, interaction.user.id, now()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            return await interaction.followup.send(
                "⚠️ You already have an active ticket for this flight. Use `/mytickets` to view it.",
                ephemeral=True,
            )

    await refresh_announcement(flight_id)
    embed = ticket_embed(ref, f)
    note = "I'll DM you when check-in opens. Make sure your DMs are open!"
    if f["checkin_ts"] <= now():
        note = "Check-in is already open. I'll DM you your check-in notice shortly."
    embed.set_footer(text=note)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def cancel_booking(ref: str, user_id: int) -> str:
    async with db_lock:
        cur = await db.execute(
            """SELECT b.id, b.status, b.flight_id, f.status AS fstatus, f.departure_ts
               FROM bookings b JOIN flights f ON f.id = b.flight_id
               WHERE b.ref = ? AND b.user_id = ?""",
            (ref, user_id),
        )
        row = await cur.fetchone()
        if not row:
            return "❌ Ticket not found. Check the reference with `/mytickets`."
        if row["status"] != "active":
            return "⚠️ That ticket is already cancelled."
        if row["fstatus"] != "scheduled" or row["departure_ts"] <= now():
            return "❌ This flight has already departed, so the ticket can't be cancelled."
        await db.execute(
            "UPDATE bookings SET status = 'cancelled', cancelled_ts = ? WHERE id = ?", (now(), row["id"])
        )
        await db.commit()
    await refresh_announcement(row["flight_id"])
    return f"✅ Ticket `{ref}` has been cancelled. Your seat was released."


class ConfirmCancel(discord.ui.View):
    def __init__(self, user_id: int, ref: str):
        super().__init__(timeout=60)
        self.user_id, self.ref = user_id, ref

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Yes, cancel my ticket", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        result = await cancel_booking(self.ref, self.user_id)
        await interaction.response.edit_message(content=result, embed=None, view=None)

    @discord.ui.button(label="Keep ticket", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="👍 Your ticket was kept.", embed=None, view=None)


# --------------------------------------------------------------------------- #
# Bot + permission check
# --------------------------------------------------------------------------- #
class FlightBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global db
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()

        self.add_dynamic_items(BookButton)  # re-attach persistent buttons after restart

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        scheduler.start()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


bot = FlightBot()


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        m = interaction.user
        if not isinstance(m, discord.Member):
            return False
        if m.guild_permissions.manage_guild:
            return True
        return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in m.roles))

    return app_commands.check(predicate)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "🔒 You don't have permission to use this command."
    else:
        log.exception("Command error", exc_info=error)
        msg = "⚠️ Something went wrong. Please try again."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------------- #
# Staff commands: /flight ...
# --------------------------------------------------------------------------- #
flight_group = app_commands.Group(name="flight", description="Flight management (staff)", guild_only=True)
bot.tree.add_command(flight_group)


@flight_group.command(name="create", description="Announce a new flight")
@staff_only()
@app_commands.describe(
    code="Flight number, e.g. AX101",
    origin="Departure airport / city",
    destination="Arrival airport / city",
    departure="Departure time in UTC: YYYY-MM-DD HH:MM",
    seats="Number of seats",
    checkin_minutes="Minutes before departure that check-in opens (default 30)",
    price="Ticket price (optional)",
    aircraft="Aircraft type (optional)",
    channel="Where to announce (defaults to the configured announcement channel)",
)
async def flight_create(
    interaction: discord.Interaction,
    code: str,
    origin: str,
    destination: str,
    departure: str,
    seats: app_commands.Range[int, 1, 1000],
    checkin_minutes: app_commands.Range[int, 0, 10080] = 30,
    price: app_commands.Range[int, 0, 100_000_000] = 0,
    aircraft: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
):
    await interaction.response.defer(ephemeral=True)

    dep_ts = parse_departure(departure)
    if dep_ts is None:
        return await interaction.followup.send(
            "❌ Invalid time. Use `YYYY-MM-DD HH:MM` in **UTC**, e.g. `2026-11-20 18:30`.", ephemeral=True
        )
    if dep_ts <= now():
        return await interaction.followup.send("❌ Departure must be in the future.", ephemeral=True)

    target = channel
    if target is None and ANNOUNCE_CHANNEL_ID:
        target = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    target = target or interaction.channel

    cur = await db.execute(
        """INSERT INTO flights (code, origin, destination, aircraft, price, seats,
                                departure_ts, checkin_ts, guild_id, channel_id, host_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (code.upper().strip(), origin.strip(), destination.strip(), aircraft, price, seats,
         dep_ts, dep_ts - checkin_minutes * 60, interaction.guild_id, target.id, interaction.user.id),
    )
    await db.commit()
    flight_id = cur.lastrowid
    f = await get_flight(flight_id)

    try:
        ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else None
        msg = await target.send(
            content=ping,
            embed=flight_embed(f, 0),
            view=make_view(flight_id, False),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException as exc:
        await db.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
        await db.commit()
        return await interaction.followup.send(
            f"❌ I couldn't post in {target.mention}: {exc}. Check my permissions there.", ephemeral=True
        )

    await db.execute("UPDATE flights SET message_id = ? WHERE id = ?", (msg.id, flight_id))
    await db.commit()
    await interaction.followup.send(f"✅ Flight **{f['code']}** announced in {target.mention} (ID {flight_id}).", ephemeral=True)


@flight_group.command(name="cancel", description="Cancel a flight and notify all passengers")
@staff_only()
@app_commands.describe(flight_id="Flight ID (shown in /flights)", reason="Reason shown to passengers")
async def flight_cancel(interaction: discord.Interaction, flight_id: int, reason: str = "No reason provided"):
    await interaction.response.defer(ephemeral=True)

    async with db_lock:
        f = await get_flight(flight_id)
        if not f:
            return await interaction.followup.send("❌ Flight not found.", ephemeral=True)
        if f["status"] != "scheduled":
            return await interaction.followup.send("⚠️ That flight is not active.", ephemeral=True)
        cur = await db.execute(
            "SELECT user_id, ref FROM bookings WHERE flight_id = ? AND status = 'active'", (flight_id,)
        )
        passengers = await cur.fetchall()
        await db.execute("UPDATE flights SET status = 'cancelled' WHERE id = ?", (flight_id,))
        await db.execute(
            "UPDATE bookings SET status = 'flight_cancelled', cancelled_ts = ? "
            "WHERE flight_id = ? AND status = 'active'",
            (now(), flight_id),
        )
        await db.commit()

    await refresh_announcement(flight_id)

    sent = 0
    for p in passengers:
        e = ticket_embed(p["ref"], f, title="🚫 Your flight has been cancelled")
        e.color = discord.Color(RED)
        e.add_field(name="Reason", value=reason, inline=False)
        if await dm_user(p["user_id"], e):
            sent += 1
        await asyncio.sleep(0.5)
    await interaction.followup.send(
        f"✅ Flight **{f['code']}** cancelled. Notified {sent}/{len(passengers)} passengers by DM.", ephemeral=True
    )


@flight_group.command(name="passengers", description="List passengers booked on a flight")
@staff_only()
async def flight_passengers(interaction: discord.Interaction, flight_id: int):
    f = await get_flight(flight_id)
    if not f:
        return await interaction.response.send_message("❌ Flight not found.", ephemeral=True)
    cur = await db.execute(
        "SELECT user_id, ref FROM bookings WHERE flight_id = ? AND status = 'active' ORDER BY booked_ts",
        (flight_id,),
    )
    rows = await cur.fetchall()
    lines = [f"{i}. <@{r['user_id']}> · `{r['ref']}`" for i, r in enumerate(rows, 1)] or ["No passengers yet."]
    e = discord.Embed(
        title=f"👥 Passengers · {f['code']} ({len(rows)}/{f['seats']})",
        description="\n".join(lines)[:4000],
        color=BLUE,
    )
    await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# --------------------------------------------------------------------------- #
# Member commands (all replies are private)
# --------------------------------------------------------------------------- #
@bot.tree.command(name="flights", description="See upcoming flights")
@app_commands.guild_only()
async def flights_cmd(interaction: discord.Interaction):
    cur = await db.execute(
        """SELECT f.*, (SELECT COUNT(*) FROM bookings b WHERE b.flight_id = f.id AND b.status = 'active') AS booked
           FROM flights f WHERE f.status = 'scheduled' AND f.departure_ts > ?
           ORDER BY f.departure_ts LIMIT 15""",
        (now(),),
    )
    rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No upcoming flights right now.", ephemeral=True)
    lines = []
    for r in rows:
        link = jump_url(r)
        title = f"[**{r['code']}**]({link})" if link else f"**{r['code']}**"
        lines.append(
            f"`#{r['id']}` {title} · {r['origin']} → {r['destination']}\n"
            f"　<t:{r['departure_ts']}:f> · {max(r['seats'] - r['booked'], 0)} seats left"
        )
    e = discord.Embed(title="🛫 Upcoming flights", description="\n".join(lines), color=GREEN)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="mytickets", description="View your ticket history (only you can see this)")
@app_commands.guild_only()
async def mytickets(interaction: discord.Interaction):
    cur = await db.execute(
        """SELECT b.ref, b.status, b.booked_ts, f.code, f.origin, f.destination,
                  f.departure_ts, f.status AS fstatus
           FROM bookings b JOIN flights f ON f.id = b.flight_id
           WHERE b.user_id = ? ORDER BY b.booked_ts DESC LIMIT 15""",
        (interaction.user.id,),
    )
    rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("You haven't booked any tickets yet.", ephemeral=True)

    def label(r) -> str:
        if r["status"] == "cancelled":
            return "❌ Cancelled by you"
        if r["status"] == "flight_cancelled":
            return "🚫 Flight cancelled"
        if r["fstatus"] == "departed" or r["departure_ts"] <= now():
            return "✅ Completed"
        return "🟢 Active"

    lines = [
        f"`{r['ref']}` · **{r['code']}** {r['origin']} → {r['destination']}\n"
        f"　<t:{r['departure_ts']}:f> · {label(r)}"
        for r in rows
    ]
    e = discord.Embed(title="🧾 Your ticket history", description="\n".join(lines), color=BLUE)
    e.set_footer(text="Showing your 15 most recent tickets · Cancel with /cancelticket")
    await interaction.response.send_message(embed=e, ephemeral=True)


async def ticket_autocomplete(interaction: discord.Interaction, current: str):
    cur = await db.execute(
        """SELECT b.ref, f.code, f.origin, f.destination
           FROM bookings b JOIN flights f ON f.id = b.flight_id
           WHERE b.user_id = ? AND b.status = 'active' AND f.status = 'scheduled' AND f.departure_ts > ?
           ORDER BY f.departure_ts LIMIT 25""",
        (interaction.user.id, now()),
    )
    rows = await cur.fetchall()
    choices = []
    for r in rows:
        name = f"{r['ref']} · {r['code']} {r['origin']} → {r['destination']}"
        if current.lower() in name.lower():
            choices.append(app_commands.Choice(name=name[:100], value=r["ref"]))
    return choices


@bot.tree.command(name="cancelticket", description="Cancel one of your tickets (private)")
@app_commands.guild_only()
@app_commands.describe(ticket="Pick the ticket to cancel")
@app_commands.autocomplete(ticket=ticket_autocomplete)
async def cancelticket(interaction: discord.Interaction, ticket: str):
    ref = ticket.strip().upper()
    cur = await db.execute(
        """SELECT b.status, f.*
           FROM bookings b JOIN flights f ON f.id = b.flight_id
           WHERE b.ref = ? AND b.user_id = ?""",
        (ref, interaction.user.id),
    )
    row = await cur.fetchone()
    if not row or row["status"] != "active":
        return await interaction.response.send_message("❌ No active ticket found with that reference.", ephemeral=True)
    await interaction.response.send_message(
        content="Are you sure you want to cancel this ticket?",
        embed=ticket_embed(ref, row, title="🎟️ Ticket to cancel"),
        view=ConfirmCancel(interaction.user.id, ref),
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Background scheduler: check-in DMs + auto-departure
# --------------------------------------------------------------------------- #
@tasks.loop(seconds=30)
async def scheduler():
    try:
        await send_checkin_notices()
        await mark_departed()
    except Exception:
        log.exception("Scheduler error")


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


async def send_checkin_notices():
    cur = await db.execute(
        """SELECT b.id AS bid, b.ref, b.user_id, b.flight_id
           FROM bookings b JOIN flights f ON f.id = b.flight_id
           WHERE b.status = 'active' AND b.notified = 0
             AND f.status = 'scheduled' AND f.checkin_ts <= ? AND f.departure_ts > ?""",
        (now(), now()),
    )
    pending = await cur.fetchall()
    cache = {}
    for p in pending:
        f = cache.get(p["flight_id"]) or await get_flight(p["flight_id"])
        cache[p["flight_id"]] = f

        e = ticket_embed(p["ref"], f, title="🛄 Check-in is now open!")
        e.color = discord.Color(GREEN)
        e.description = f"Check-in for **{f['code']}** ({f['origin']} → {f['destination']}) is open. Head to the check-in area now!"
        link = jump_url(f)
        if link:
            e.add_field(name="Flight announcement", value=f"[Jump to message]({link})", inline=False)

        ok = await dm_user(p["user_id"], e)
        await db.execute("UPDATE bookings SET notified = 1 WHERE id = ?", (p["bid"],))
        await db.commit()
        if not ok:
            await log_to_staff(f"⚠️ Couldn't DM <@{p['user_id']}> their check-in notice for {f['code']} (DMs closed).")
        await asyncio.sleep(0.5)  # be kind to the rate limit


async def mark_departed():
    cur = await db.execute(
        "SELECT id FROM flights WHERE status = 'scheduled' AND departure_ts <= ?", (now(),)
    )
    for r in await cur.fetchall():
        await db.execute("UPDATE flights SET status = 'departed' WHERE id = ?", (r["id"],))
        await db.commit()
        await refresh_announcement(r["id"])


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
