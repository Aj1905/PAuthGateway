# Linuxの外向き遮断を実カーネルで検査する

ホストの規則を変更せず、外部ネットワークを持たない専用コンテナ内で
`gateway/deploy/egress_lockdown.sh` の適用・解除と
`gateway/operator/health.py` の検出を測る。Dockerの起動が必要。

リポジトリ直下で実行する。

```sh
docker build --pull=false -t pauth-egress-probe:codex docs/lab/egress_probe
docker run --rm --network none --cap-add NET_ADMIN \
  --security-opt no-new-privileges:true --read-only \
  --tmpfs /tmp:rw,nosuid,nodev --tmpfs /run:rw,nosuid,nodev \
  --pids-limit 128 --memory 256m --cpus 1 \
  --mount "type=bind,source=$PWD/gateway/deploy/egress_lockdown.sh,target=/probe/egress_lockdown.sh,readonly" \
  --mount "type=bind,source=$PWD/gateway/operator/health.py,target=/probe/health.py,readonly" \
  pauth-egress-probe:codex
```

コンテナの利用者61001がTCP/UDPソケットで実際に送受信する。受信側は別利用者(root)。
ホストの8091番ポートや実験用MCPには接続しない。

- 適用前: ゲートウェイ相当の宛先、別ポート、別IPv4アドレス、UDP、IPv6の全5経路が通る。
- 適用中: 指定のIPv4アドレス・TCPポートだけ通り、残り4経路は通らない。
- 解除後: 全5経路が再び通る。
- 健全性検査: 適用中はok、削除後はfail。

nftablesを優先する経路と、nftをPATHに含めないiptables経路を同じ実スクリプトで検査する。
1項目でも期待と異なると終了値は非0。IPv6を含め、使えない経路を成功として飛ばさない。

これはLinux内の分離されたネットワークでの部品検査であり、macOSのpf、
ホスト全体の隔離、実インターネット宛て通信、人間確認付きの全体実験の証拠ではない。
結果は `board/evidence-codex-egress.json`。
