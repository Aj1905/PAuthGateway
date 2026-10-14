# _gdrive/ — 正本は Google Drive

このフォルダの中身は README.md 以外 git に入らない。再現できないが公開もしない作業物 (計画メモ、原稿、実験の生データなど) を置く。
正本は Google Drive の `repo-local/PAuthGateway/` (rclone の remote `gdrive:`)。Google Drive のデスクトップ版は使わない。

## 取り出す

```
rclone copy gdrive:repo-local/PAuthGateway/ _gdrive/ --exclude README.md
```

## 送る (編集したら)

```
rclone copy _gdrive/ gdrive:repo-local/PAuthGateway/ --exclude README.md
```

## 差分を見る

```
rclone check _gdrive/ gdrive:repo-local/PAuthGateway/ --exclude README.md
```

片方向の copy だけ使う (sync は片側を消せる)。再生成できるもの (.venv、ビルド物、モデル重み) はここに入れない。
