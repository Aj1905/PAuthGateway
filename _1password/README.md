# _1password/ — 正本は 1Password

このフォルダの中身は README.md 以外 git に入らない。正本は 1Password の保管庫 `dev`、項目 `PAuthGateway` (個人アカウント my.1password.com)。
値そのものをここに書き残さず、必ず 1Password から取り出す。

## 自動同期

`git push` のとき手元の内容が 1Password へ (pre-push)、`git pull` で新しいコミットが来たとき 1Password から手元へ (post-merge) 同期される。新しい方が勝つ。
手で動かすなら `repo-side-sync push` / `repo-side-sync pull` / `repo-side-sync status` (~/bin)。差分の無い pull では自動では走らない。

## 取り出す (端末を新しくしたとき)

```
OP_ACCOUNT=my.1password.com op inject -i .env.tpl -o _1password/.env && ln -sfn _1password/.env .env
```

## 編集する

値は 1Password 側で変え、そのあと上の取り出しをやり直す。

```
OP_ACCOUNT=my.1password.com op item edit PAuthGateway --vault dev 'KEY=新しい値'
```

手元の .env をまとめて登録するなら `env-to-op PAuthGateway _1password/.env` (~/bin)。鍵を増やしたら .env.tpl に `KEY=op://dev/PAuthGateway/KEY` を足す。

## .env 以外のファイル

書類項目として入れる。題名は `PAuthGateway/<ファイル名>`。

```
OP_ACCOUNT=my.1password.com op document create _1password/<file> --vault dev --title 'PAuthGateway/<file>'
OP_ACCOUNT=my.1password.com op document get 'PAuthGateway/<file>' --vault dev --out-file _1password/<file>
```
