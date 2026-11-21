#!/usr/bin/env bash
# ==============================================================================
# NotANerd (@ssh_bugs) Agent Management CLI
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

COMPOSE="docker compose -f compose.prod.yaml"

show_help() {
  echo "NotANerd (@ssh_bugs) Unified Agent CLI"
  echo ""
  echo "Usage: ./agent.sh [command] [options]"
  echo ""
  echo "Commands:"
  echo "  status               Show system health, docker containers, and monitor report"
  echo "  video [url]          Download viral video, generate Hinglish caption & post live"
  echo "  video-auto           Auto-scan viral handles and post top high-impression video"
  echo "  video-list           Scan and list high-impression video candidates without posting"
  echo "  quote <url> [style]  Quote-tweet a post live with AI take (styles: sarcastic, bakchodi, rage_bait)"
  echo "  reply <url> [style]  Reply to a tweet live with AI comment (styles: sarcastic, bakchodi, rage_bait)"
  echo "  post                 Trigger immediate original tweet cycle"
  echo "  logs                 Tail live application logs"
  echo "  restart              Restart all agent containers (app, monitor, db)"
  echo ""
}

case "$1" in
  status)
    echo "=================================================="
    echo "1. DOCKER CONTAINERS"
    echo "=================================================="
    $COMPOSE ps
    echo ""
    echo "=================================================="
    echo "2. AGENT MONITOR HEALTH"
    echo "=================================================="
    $COMPOSE exec -T app python -m scripts.monitor
    ;;

  video)
    if [ -n "$2" ]; then
      echo "[*] Downloading video from $2, generating Hinglish caption & posting live to @ssh_bugs..."
      $COMPOSE exec -T app python -m scripts.repost_video --tweet "$2" --live
    else
      echo "[*] Auto-scanning viral handles, generating Hinglish caption & posting top video live..."
      $COMPOSE exec -T app python -m scripts.repost_video --auto --live
    fi
    ;;

  video-auto)
    echo "[*] Auto-scanning viral handles, generating Hinglish caption & posting top video live..."
    $COMPOSE exec -T app python -m scripts.repost_video --auto --live
    ;;

  video-list)
    echo "[*] Scanning viral handles for high-impression video candidates..."
    $COMPOSE exec -T app python -m scripts.repost_video --list
    ;;

  quote)
    if [ -z "$2" ]; then
      echo "Error: Tweet URL required. Usage: ./agent.sh quote <tweet_url> [style]"
      exit 1
    fi
    STYLE="${3:-quote_tweet}"
    echo "[*] Quote-tweeting $2 with style: $STYLE..."
    $COMPOSE exec -T app python -m scripts.engage --tweet "$2" --action quote --style "$STYLE" --live
    ;;

  reply)
    if [ -z "$2" ]; then
      echo "Error: Tweet URL required. Usage: ./agent.sh reply <tweet_url> [style]"
      exit 1
    fi
    STYLE="${3:-sarcastic}"
    echo "[*] Replying to $2 with style: $STYLE..."
    $COMPOSE exec -T app python -m scripts.engage --tweet "$2" --action reply --style "$STYLE" --live
    ;;

  post)
    echo "[*] Triggering immediate original post cycle..."
    $COMPOSE exec -T app python -m scripts.cycle --live
    ;;

  logs)
    $COMPOSE logs -f --tail=50 app
    ;;

  restart)
    echo "[*] Restarting agent containers..."
    $COMPOSE restart
    $COMPOSE ps
    ;;

  *)
    show_help
    ;;
esac

