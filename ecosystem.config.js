module.exports = {
  apps: [
    {
      name: "miraq-chat-staging",
      script: "gunicorn",
      interpreter: "none", // gunicorn is the executable, not a JS file
      args: "server:app --bind 0.0.0.0:5009 --workers 4 --worker-class gthread --threads 4 --timeout 120 --access-logfile - --log-level info",
      cwd: "/path/to/your/app",
      instances: 1, // gunicorn forks the workers, NOT pm2
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
