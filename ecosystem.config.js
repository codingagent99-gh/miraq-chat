module.exports = {
  apps: [
    {
      name: "miraq-multi",
      script: "/home/apps/varchaswi/miraq-chat/.venv/bin/gunicorn",
      interpreter: "none",
      // Raise this only after confirming free memory AND idle cores.
      args: "server:app --bind 0.0.0.0:5017 --workers 2 --worker-class gthread --threads 4 --timeout 120 --access-logfile - --log-level info",
      cwd: "/home/apps/varchaswi/miraq-chat-shopify-multi",
      instances: 1,
      autorestart: true,
      max_memory_restart: "2G",
      env: {
        DEBUG: "false",
        USE_RELOADER: "false",
        TIMING_LOG_ENABLED: "true",
      },
    },
    // ── Data-retention jobs (privacy policy, Section 6) ───────────────────────
    // Run once at `pm2 start`, then again at each cron_restart time. They exit
    // after each run, so PM2 shows them as "stopped" in between — that is
    // normal (autorestart: false). Output goes under the project's own logs/.
    {
      name: "miraq-multi-purge-logs",
      script: "/home/apps/varchaswi/miraq-chat-shopify-multi/purge_logs.py",
      interpreter: "/home/apps/varchaswi/miraq-chat/.venv/bin/python",
      cwd: "/home/apps/varchaswi/miraq-chat-shopify-multi",
      cron_restart: "30 2 * * *",
      autorestart: false,
      out_file: "/home/apps/varchaswi/miraq-chat-shopify-multi/logs/purge.log",
      error_file:
        "/home/apps/varchaswi/miraq-chat-shopify-multi/logs/purge.log",
    },
    {
      name: "miraq-multi-purge-chat",
      script:
        "/home/apps/varchaswi/miraq-chat-shopify-multi/purge_chat_history.py",
      interpreter: "/home/apps/varchaswi/miraq-chat/.venv/bin/python",
      cwd: "/home/apps/varchaswi/miraq-chat-shopify-multi",
      cron_restart: "45 2 * * *",
      autorestart: false,
      out_file: "/home/apps/varchaswi/miraq-chat-shopify-multi/logs/purge.log",
      error_file:
        "/home/apps/varchaswi/miraq-chat-shopify-multi/logs/purge.log",
    },
  ],
};
