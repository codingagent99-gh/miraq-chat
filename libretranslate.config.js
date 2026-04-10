module.exports = {
  apps: [
    {
      name: "libretranslate",
      script: "/home/apps/varchaswi/miraq-chat/.venv/bin/libretranslate",
      interpreter: "none",
      args: "--host 0.0.0.0 --port 5012 --load-only en,es",
      watch: false,
      autorestart: true,
      max_restarts: 5,
      restart_delay: 10000,
      kill_timeout: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      error_file: "/home/apps/varchaswi/logs/libretranslate-error.log",
      out_file: "/home/apps/varchaswi/logs/libretranslate-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
