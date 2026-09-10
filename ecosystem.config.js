module.exports = {
  apps: [
    {
      name: "miraq-chat-staging",
      script: "/home/apps/varchaswi/miraq-chat-staging/.venv/bin/gunicorn",
      interpreter: "none",
      args: "server:app --bind 0.0.0.0:5009 --workers 4 --worker-class gthread --threads 4 --timeout 120 --access-logfile - --log-level info",
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
