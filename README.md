# Prop Edge Board (Phase 1: NBA)

Compares Kalshi NBA player props with de-vigged sportsbook odds and shows any play where Kalshi is cheaper than the books' fair price, after Kalshi's fee.

## Files
| File | What it does |
|---|---|
| `index.html` | The dashboard page |
| `scripts/build_nba.py` | Pulls Kalshi + odds, does the math, writes `data/nba.json` |
| `.github/workflows/nba.yml` | Runs the script on a schedule and saves the results |
| `data/` | Created automatically by the first run. Don't edit. |

## One-time setup (about 15 minutes)

### 1. Get a free PropLine key
1. Go to **prop-line.com**, enter your email, and copy the API key it shows you. No card needed. Free tier = 1,000 requests a day.
2. Optional backup: sign up for the free plan at **the-odds-api.com** and copy that key too. It only gets used if PropLine fails, and its 500 free credits a month cover very few prop pulls.

### 2. Make the repo
1. On GitHub, click **+** (top right) → **New repository**.
2. Name it (for example `prop-edge`), choose **Public**, tick **Add a README file**, click **Create repository**.
   It must be public: GitHub Pages and unlimited Actions minutes are free only for public repos.

### 3. Add the files
1. In the repo, click **Add file → Upload files**. Drag in `index.html`, `README.md` and the whole `scripts` folder. Click **Commit changes**.
2. Click **Add file → Create new file**. In the name box type exactly `.github/workflows/nba.yml` (typing the `/` makes the folders). Paste the full contents of `nba.yml`. Click **Commit changes**.
   (Use this method because Mac and Windows hide folders that start with a dot, so drag-and-drop often skips them.)

### 4. Store the key as a secret (never visible on the page)
1. Repo → **Settings** → left menu **Secrets and variables → Actions** → **New repository secret**.
2. Name: `PROPLINE_API_KEY`. Secret: paste your PropLine key. **Add secret**.
3. Optional: add a second secret named `ODDS_API_KEY` with The Odds API key.

### 5. Let the workflow save data
Repo → **Settings → Actions → General** → scroll to **Workflow permissions** → choose **Read and write permissions** → **Save**.

### 6. Turn on GitHub Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: **main**, folder **/ (root)** → **Save**.

### 7. First run
1. Repo → **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **NBA props update** on the left → **Run workflow** → **Run workflow**.
3. Wait about a minute for the green check. A new `data` folder appears in the repo.
4. Wait 1–2 more minutes for Pages to rebuild, then open `https://YOUR-USERNAME.github.io/prop-edge/`.

## How to check it worked
- The four status cards at the top all show times, not "Unavailable".
- Open **Diagnostics** at the bottom: "Errors" should say none. "Kalshi markets per series" shows how many props Kalshi has for each stat.
- If Kalshi has props tonight, the table or the "Line mismatches" box should have rows. An empty table can be correct: it means no play clears your filters. Tick **Show every matched prop** to see everything that matched.
- Click any row to see each book's Over/Under odds and Kalshi's full bid/ask.

## Changing the schedule
The run times live in `.github/workflows/nba.yml` (lines starting with `- cron:`). They are in UTC: `"7 22 * * *"` means 22:07 UTC = 5:07 PM Eastern in winter (6:07 PM in summer). To change one:
1. Open the file on GitHub, click the pencil icon, edit the numbers (minute first, then hour), commit.
2. Also edit `SCHEDULE_UTC` near the top of `scripts/build_nba.py` the same way, so the page's "Next automatic run" stays correct.

Each run costs about 1 + (number of NBA games with Kalshi props) PropLine requests, so roughly 10–16 per run and under 60 a day at three runs. On days with no Kalshi NBA props it costs zero.

## Things that can't be done for free (and what this does instead)
- **The Refresh button can't pull new sportsbook odds.** A static page can't trigger the workflow without exposing a GitHub token. Refresh reloads the newest automated data and tries to fetch live Kalshi prices from your browser. For fresh odds, use **Actions → Run workflow**.
- **Live Kalshi from the browser may be blocked** if Kalshi doesn't allow browser requests. The page says so plainly and falls back to the last automated Kalshi prices.
- **GitHub disables scheduled workflows in public repos after 60 days without commits.** The data commits keep it alive during the season. After a long break, re-enable it in the Actions tab.
