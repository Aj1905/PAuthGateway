# .env の雛形。秘密の値は 1Password に置き、ここには参照だけを書く (push してよい)。
# 実体を作る:  op inject -i .env.tpl -o .env
# 実行時だけ注入する:  op run --env-file=.env.tpl -- <コマンド>
# 初回登録 (手元の .env から):  env-to-op PAuthGateway

# A1 (code-generation) ステップで使う OpenAI の鍵。shopping スイートと offline テストには不要。
OPENAI_API_KEY=op://dev/PAuthGateway/OPENAI_API_KEY

# A1 imperative-code generation に使うモデル (論文は gpt-4.1)。秘密ではないので値を直接書く。
PAUTH_MODEL=gpt-4.1
