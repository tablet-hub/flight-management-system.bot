# Flight Booking Discord Bot

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in (never commit `.env`).
3. In the Developer Portal, invite the bot with scopes `bot` + `applications.commands`
   and permissions: View Channel, Send Messages, Embed Links.
   No privileged intents are needed.
4. `python bot.py`

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
- Data lives in `flights.db` (SQLite). Back it up if you host on a VPS.
