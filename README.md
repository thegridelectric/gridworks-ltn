# gridworks-ltn

The Leaf Transactive Node as a gwbase actor. One LTN serves one house:
it hears its scada, requests a plan from a FLO, bids into its market
maker's slot, and tells the scada the contract that clears.

## The seam

The scada speaks MQTT; this actor speaks AMQP. They meet on the broker's
`amq.topic`:

- scada to LTN: the scada publishes a wrapped message on
  `gw/<scada-alias-hyphenated>/to/ltn/<type-hyphenated>`; the actor's
  queue is bound to `gw.<scada-alias-hyphenated>.to.ltn.#`, its own
  scada's alias and no other house's.
- LTN to scada: the actor publishes a wrapped message on
  `gw.<ltn-alias-hyphenated>.to.s.<type-hyphenated>`, which the scada
  hears as `gw/<ltn-alias-hyphenated>/to/s/<type-hyphenated>`. `s` is the
  scada's node name on the wire.

Every scada message that asks for an ack gets one, inside the scada's
5 s timer, and the actor pings the scada every 60 s so the scada's link
stays Active.

## The GNodes

The FLO and the market maker are gwbase GNodes, and the LTN is one too:
it declares `GwGNodeClass.LeafTransactiveNode` and boots on the
`g.node.gt.json` at `GWLTN_G_NODE_PATH`, whose alias and class must
agree. The FLO and the maker are reached on the class fabric with no
wrapper: `flo.params` goes to the FLO GNode named by `GWLTN_FLO_ALIAS` as
a direct message over the LeafTransactiveNode-to-Flo edge and its
`flo.recommendation` comes back direct over the edge back; the bid goes
direct to the maker named by the ops word. The maker's `latest.price`
broadcast arrives on the maker's own mic exchange.

## The house

The LTN boots on the house's two authored artifacts in its config dir,
the layout and its operational params, the same pair the scada runs on.
The layout names the LTN and the scada, so the actor's alias must be
the layout's LTN GNode and the scada it hears is the layout's scada.

## The vocabulary

Every word the LTN speaks or hears is a class of its vendored Sema
snapshot, `src/gwltn/sema` (GENERATED; never hand-edited), seeded by
`src/gwltn/sema_seed_request.yaml` at the one version of each word the
scada speaks, and regenerated from a sibling `../sema` checkout with
`scripts/regen_sema_snapshot.sh`. Local class names drop a leading
`gw.`/`gw1.`/`gw2.` segment. Dict and JSON forms are the PascalCase wire
form (`to_dict`, `from_dict`, `to_bytes`, `from_bytes`); fields are
snake_case. The two words on the scada's wire with no sema word yet,
`no.new.contract.warning` and `send.snap`, are local classes in
`src/gwltn/words.py` in the snapshot's style, each retired by the coined
word.

## The FLO

At the bid minute of each hour the LTN measures the store, builds one
`flo.params` and sends it to the FLO GNode. The recommendation that
answers it (same bidder, same slot) is kept for the clearing, copied to
the scada, and the bid goes off it to the maker. A recommendation for
any other request is dropped. With no answer by the top of the hour the
LTN places no bid for the slot.

## Run

```shell
uv sync --all-groups
cp template.env .env                # and fill in
uv run gwltn rabbit
```

`./ci.sh` is the gate (ruff, pyright, pytest); the GitHub workflow runs
it and nothing else, so a local pass is a CI pass. The broker tests need the
gwbase dev broker (`gw-dev-rabbit`: AMQP on 5672, MQTT on 1885) and
self-skip without it. `scripts/dev-g.node.gt.json` is the dev identity
the tests boot with; a deployed LTN has its own at
`~/.config/gridworks/ltn/g.node.gt.json`.

## Deploy

One login `ltn` per box, one unit `service/gwltn-rabbit.service` per
house, one FIS principal per LTN. The checkout on the box is a pushed
`main` SHA.
