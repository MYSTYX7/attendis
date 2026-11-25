# Moderator Attendance Bot

## Setup
1. Supabase: run `schema.sql` in the SQL editor. Copy Project URL + **service_role** key.
2. Discord Developer Portal → Bot → enable **Presence Intent** and **Server Members Intent**.
3. Invite with scopes `bot applications.commands`, permissions: Send Messages, Embed Links, Attach Files.
4. `cp .env.example .env`, fill it, then `pip install -r requirements.txt && python bot.py`

## How it works
- Attendance records older than 30 days are deleted automatically (checked every 6 hours).
- Admin (Manage Server) adds moderators and slots. Every minute the bot checks if each moderator
  is available (online status and/or in a voice channel, see `/config mode`).
- Once they accumulate `min_minutes` (default 15) inside the slot -> auto-marked **present**.
- Slot ends with less than that -> **absent** (posted to the log channel).
- Manual marking (`/attendance mark`) is only allowed once the slot has been running `min_minutes`
  (Administrators can pass `force:True`).

## Commands
Admin (Manage Server):
- `/mod add|remove|list`
- `/slot add|remove|list`  (day: Every day / Weekdays / Mon–Sun, or a one-off `date` YYYY-MM-DD; times 24h HH:MM)
- `/attendance mark` (present / absent / excused, with note)
- `/attendance today`, `/attendance report days:30`, `/attendance export days:30` (CSV)
- `/config show`, `/config set` (min_minutes, timezone, mode, log_channel, reminder_minutes)

Everyone listed as moderator: `/myslots`, `/mystats`

## Extras included
DM reminders before a slot, absent/present log feed, excused status, one-off date slots,
per-mod stats + percentages, CSV export, per-server timezone and threshold.

## Leave
- Moderators: `/leave apply` (Whole day / Date range / Specific slots today), `/leave list`, `/leave cancel`.
- Requests are posted with Approve/Reject buttons in the channel set by `/config set leave_channel`
  (falls back to the log channel). Pings the approvers.
- Admin chooses who can decide: `/config approver_add` / `approver_remove` (roles or members).
  Anyone with Manage Server can always decide.
- Approved leave marks the covered slots **excused**, so they don't count toward attendance %.
  A slot already auto-marked present stays present.
