name: ClaudeBot Paper Trader

on:
  schedule:
    - cron: '0 */3 * * *'
  workflow_dispatch:

jobs:
  run-bot:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install anthropic requests ddgs

      - name: Run bot (single scan)
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHANNEL_ID: ${{ secrets.TELEGRAM_CHANNEL_ID }}
          TELEGRAM_PERSONAL_ID: ${{ secrets.TELEGRAM_PERSONAL_ID }}
        run: python claudebot.py --single-scan

      - name: Commit updated log back to repo
        run: |
          git config user.name "claudebot"
          git config user.email "claudebot@users.noreply.github.com"
          git add claudebot_log.json
          git diff --staged --quiet || git commit -m "bot: update trade log [$(date '+%Y-%m-%d %H:%M')]"
          git push
