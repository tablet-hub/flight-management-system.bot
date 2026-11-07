# Flight Booking Discord Bot

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in (never commit `.env`).
3. In the Developer Portal, invite the bot with scopes `bot` + `applications.commands`
   and permissions: View Channel, Send Messages, Embed Links.
   No privileged intents are needed.
4. Set Railway's start command to `python bot.py`.
5. In Railway Variables, set `DISCORD_TOKEN` to your **newly regenerated** bot token. Set `GUILD_ID`, `STAFF_ROLE_ID`, `ANNOUNCE_CHANNEL_ID`, `PING_ROLE_ID`, and `LOG_CHANNEL_ID` to the numeric IDs shown in Discord (the defaults in `.env.example` are starter values).
6. Run locally with `python bot.py` after setting the same variables in `.env`.

## Commands
| Command | Who | What |
|---|---|---|
| `/flight create` | Staff | Announce a flight with a Book button |
| `/flight cancel` | Staff | Cancel a flight and DM all passengers |
| `/flight passengers` | Staff | List who is booked |
| `/flights` | Everyone | See upcoming flights (private) |
| `/mytickets` | Everyone | Your ticket history (private) |
| `/cancelticket` | Everyone | Cancel a ticket with confirmation (private) |

Times are entered in **UTC** as `YYYY-MM-DD HH:MM` and displayed in each member's own timezone.

## Notes
- Members must allow DMs from server members to receive check-in notices.
  If a DM fails, the bot posts a warning in `LOG_CHANNEL_ID` (if set).
- The Book button survives bot restarts.
- Data lives in `flights.db` (SQLite). Railway's ordinary container filesystem may be temporary; attach a persistent volume and set `DB_PATH` to a location on that volume if you need bookings to survive redeploys.
