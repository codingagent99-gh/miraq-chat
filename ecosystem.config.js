module.exports = {
  apps: [
    {
      name: "libretranslate",
      script: "/home/apps/varchaswi/miraq-chat/.venv/bin/python3",
      args: "-m libretranslate --host 0.0.0.0 --port 5011 --load-only en,es",
      watch: false,
      autorestart: true,
      max_restarts: 5,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      error_file: "logs/libretranslate-error.log",
      out_file: "logs/libretranslate-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
    {
      name: "chatbot-wip",
      interpreter: "python3",
      script: "server.py",
      watch: false,
      autorestart: true,
    },
  ],
};
