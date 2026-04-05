# Lovable Frontend Prompt

Paste the following into Lovable to generate your frontend:

---

Build a sports betting analytics dashboard that connects to a REST API backend. The app should have a dark theme with a modern, professional sports betting aesthetic (dark grays, accent colors of green for profit and red for loss).

## Tech Stack
- React + TypeScript + Tailwind CSS
- Recharts for charts
- React Router for navigation
- Axios or fetch for API calls
- The backend API base URL should be configurable via an environment variable `VITE_API_URL` (default: `http://localhost:8000`)

## Pages

### 1. Dashboard (`/`)
The main landing page showing:
- **Bankroll card**: Large number showing current balance, with a green/red indicator for today's change
- **Stats row**: 4 cards showing: Total Bets, Win Rate %, Total P&L, ROI %
- **Bankroll chart**: Line chart showing bankroll balance over time (from `/api/bankroll` history)
- **Upcoming Picks**: Table of top 5 upcoming predictions from `/api/dashboard` showing: matchup, sport, recommended side, EV %, confidence score, commence time
- **Recent Bets**: Table of last 10 bets from `/api/bets` showing: matchup, side, stake, odds, status (color-coded badge: green=won, red=lost, yellow=pending), P&L

### 2. Predictions (`/predictions`)
- Filter bar at top: dropdown for sport (All, NFL, NBA, MLB, NHL)
- Table showing all predictions from `/api/predictions`:
  - Columns: Sport icon, Home Team, Away Team, Game Time, Home Win %, Away Win %, Pick (highlighted), EV %, Confidence (progress bar), Best Odds, Best Book, Kelly %
  - Rows with positive EV should have a subtle green left border
  - Rows with negative EV should be dimmed
  - Sort by EV descending by default, with clickable column headers

### 3. Live Odds (`/odds`)
- Sport filter dropdown
- For each event from `/api/odds`, show a card with:
  - Matchup header (Home vs Away)
  - Game time
  - Table of bookmaker odds: columns are Bookmaker, Home, Away, Draw (if applicable)
  - Highlight the best odds in each column with green
  - Show the number of bookmakers

### 4. Bet History (`/history`)
- Filter tabs: All, Pending, Won, Lost
- Table from `/api/bets` with columns: Date, Sport, Matchup, Side, Stake, Odds, Payout, Status (badge), P&L
- Summary bar at top: Total bets, Wins, Losses, Net P&L, ROI
- Pagination or infinite scroll

### 5. Settings (`/settings`)
- Form that loads from `/api/settings` and saves via POST `/api/settings`:
  - Initial Bankroll (number input)
  - Max Bet Amount (number input)
  - Kelly Fraction (slider 0.1 to 1.0, default 0.25)
  - Min EV Threshold (number input, default 0.02)
  - Active Sports (multi-select checkboxes: NFL, NBA, MLB, NHL)
  - Auto-Betting Enabled (toggle switch with warning text)
- Save button

## Layout
- Sidebar navigation on the left with icons and labels for each page
- Collapsible on mobile (hamburger menu)
- Top bar showing "SportsBets" logo/title and current bankroll amount
- All data should auto-refresh every 60 seconds

## Additional Details
- Loading skeletons while data is fetching
- Empty states with helpful messages when no data
- Toast notifications for errors
- All monetary values formatted as USD with 2 decimal places
- All percentages shown with % symbol and 1-2 decimal places
- Timestamps shown in user's local timezone
- Responsive design for mobile, tablet, and desktop

---
