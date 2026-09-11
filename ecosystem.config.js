module.exports = {
  apps: [
    {
      name: "miraq-chat-staging",
      script: "/home/apps/varchaswi/miraq-chat/.venv/bin/gunicorn",
      interpreter: "none",
      // Raise this only after confirming free memory AND idle cores.
      args: "server:app --bind 0.0.0.0:5015 --workers 2 --worker-class gthread --threads 4 --timeout 120 --access-logfile - --log-level info",
      cwd: "/home/apps/varchaswi/miraq-chat-staging",
      instances: 1,
      autorestart: true,
      max_memory_restart: "2G",
      env: {
        DEBUG: "false",
        USE_RELOADER: "false",
        TIMING_LOG_ENABLED: "true",
      },
    },
  ],
};
