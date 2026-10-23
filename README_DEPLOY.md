\# SOC5 Attendance Bot — Deploy Guide



\## What this is

SeaTalk attendance bot for the SOC5 team. Three Python services deployed on Render.



\## Files in this folder

\- `SOC5main.py` — Flask web bot (SeaTalk webhook + admin endpoints)

\- `SOC5OT.py` — OT sync worker (writes OT rows to the OT sheet)

\- `SOC5keeper.py` — Playwright session keeper (keeps FMS login alive)

\- `requirements.txt` — Python deps

\- `Procfile` — Render start command

\- `Dockerfile.keeper` — Docker image for the keeper

\- `.gitignore` — \*\*do not delete or edit\*\*; blocks secrets from Git

\- `service\_account.json` — \*\*you add this yourself\*\* (sent separately). Never commit it.



\## Before starting

1\. `service\_account.json` in the same folder as these files

2\. A GitHub account

3\. A Render account (free tier works for the web service)



\## Step 1 — Test locally

```bash

pip install -r requirements.txt

python SOC5main.py

```

Should print `### BOT STARTING ###` and `✅ Loaded 17 users`. Ctrl+C to stop.



\## Step 2 — Push to GitHub

1\. Create a \*\*private\*\* repo on github.com

2\. In your folder:

```bash

git init

git add .

git commit -m "Initial SOC5 attendance bot"

git branch -M main

git remote add origin https://github.com/<your-username>/<repo-name>.git

git push -u origin main

```

3\. \*\*Verify `service\_account.json` was NOT pushed:\*\*

```bash

git ls-files | grep service\_account

```

Should print nothing. If it prints a line — STOP and tell Elvin.



\## Step 3 — Deploy Web Service on Render

\- New → Web Service → pick your repo

\- Name: `soc5-attendance-bot`

\- Region: Singapore

\- Runtime: Python 3

\- Build: `pip install -r requirements.txt`

\- Start: `gunicorn SOC5main:app`

\- Instance: Free



\*\*Environment variables:\*\*



| Key | Value |

|---|---|

| `SPREADSHEET\_ID` | `1s4hG4fICeL\_qOzksSg7GhR0tyTwv9HxHbcAlP8xSaME` |

| `OT\_SPREADSHEET\_ID` | `127t6dR6elGWd\_JkzZpCfSezNyQhioTwp3P1i-cUmjfI` |

| `APP\_ID` | `ODk5ODk4ODU4NDUx` |

| `APP\_SECRET` | \*(sent by Elvin)\* |

| `SEATALK\_GROUP\_ID` | `MjQ0OTE0ODU1MTkw,Mjk5NTE0MTczNzgy` — to post to more than one group, list the IDs separated by commas, e.g. `MjQ0OTE0ODU1MTkw,<second-group-id>` |

| `ADMIN\_CODE` | \*(sent by Elvin)\* |

| `ADMIN\_CODE\_2` | \*(optional)\* |

| `GOOGLE\_CREDENTIALS\_JSON` | \*(one-line JSON of service\_account.json)\* |

| `RENDER\_URL` | \*(fill after deploy)\* |



\*\*How to make GOOGLE\_CREDENTIALS\_JSON:\*\*

```bash

python -c "import json; print(json.dumps(json.load(open('service\_account.json'))))"

```

Copy the whole output — one long line — paste as the value.



\## Step 4 — Deploy Background Worker on Render (keeper)

\- New → Background Worker → same repo

\- Name: `soc5-keeper`

\- Region: Singapore

\- Runtime: \*\*Docker\*\*

\- Dockerfile Path: `./Dockerfile.keeper`

\- Instance: Starter ($7/mo — Playwright needs RAM)



\*\*Environment variables:\*\*



| Key | Value |

|---|---|

| `RENDER\_URL` | `https://soc5-attendance-bot.onrender.com` |

| `ADMIN\_CODE` | \*(same as web service)\* |

| `YOUR\_EMPLOYEE\_CODE` | \*(same as ADMIN\_CODE)\* |

| `SEATALK\_APP\_ID` | `ODk5ODk4ODU4NDUx` |

| `SEATALK\_APP\_SECRET` | \*(same as APP\_SECRET)\* |

| `SPREADSHEET\_ID` | `1s4hG4fICeL\_qOzksSg7GhR0tyTwv9HxHbcAlP8xSaME` |

| `GOOGLE\_CREDENTIALS\_JSON` | \*(same one-line JSON)\* |



\## Step 5 — Point SeaTalk webhook

SeaTalk developer console for the SOC5 app → set webhook to:

```

https://soc5-attendance-bot.onrender.com/umay

```



\## Step 6 — Test

1\. `curl https://soc5-attendance-bot.onrender.com/` → `FMS Bot Running ✅`

2\. DM the bot: `mycode` → replies with your employee code

3\. DM the bot: `fms <Full Name>` (admin) → returns FMS records

4\. Wait for real time-in → appears in SOC5 SeaTalk group



\## Notes

\- \*\*Never commit `service\_account.json`.\*\* It's in `.gitignore`.

\- Keeper must stay running 24/7 or FMS session dies.

\- Service account email must be \*\*Editor\*\* on the tracker + OT sheets, \*\*Viewer\*\* on the schedule sheet.

\- The bot must be added as a member of every SeaTalk group listed in `SEATALK\_GROUP\_ID`, or posts to that group will fail.

\- Contact Elvin if any step fails.

