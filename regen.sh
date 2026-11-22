#!/bin/sh
# regen.sh — run after a prod deploy to signal Compose that the app must restart.
# Kept as an independent step so a pure config/secret reload is cheap and explicit.
exec docker compose -f compose.prod.yaml up -d --build --force-recreate app
