// PM2 app for the IndicTrans2 translation service (translation_service/).
// Uses its OWN virtualenv (.venv-indictrans2) — torch/transformers must not
// go into the chat backend's .venv.
//
// One worker on purpose: each worker loads both models into memory
// (~1-2 GB for the dist-200M pair on CPU). Threads share the loaded models;
// generate() is serialised per direction inside the service.
module.exports = {
  apps: [
    {
      name: "indictrans2",
      script: "/home/apps/varchaswi/miraq-chat/.venv-indictrans2/bin/gunicorn",
      interpreter: "none",
      args: "indictrans2_server:app --bind 127.0.0.1:5018 --workers 1 --threads 4 --timeout 180",
      cwd: "/home/apps/varchaswi/miraq-chat/translation_service",
      watch: false,
      autorestart: true,
      max_restarts: 5,
      restart_delay: 10000,
      kill_timeout: 5000,
      max_memory_restart: "3G",
      env: {
        PYTHONUNBUFFERED: "1",
        IT2_DEVICE: "cpu",
        IT2_NUM_BEAMS: "3",
      },
      error_file: "/home/apps/varchaswi/logs/indictrans2-error.log",
      out_file: "/home/apps/varchaswi/logs/indictrans2-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
