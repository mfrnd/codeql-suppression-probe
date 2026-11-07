# PyKeePassGate

<!-- One of Python's built-in functions, named the way Python names it. -->
<!-- cspell:ignore repr -->

The reference client for [KeePassGate](https://github.com/mfrnd/KeePassGate), a KeePass 2.x plugin
that serves a small specification-defined API over mutual TLS so that automation and AI agents can
each be given a narrow, revocable, audited slice of a database.

**Reference implementation** means the code here is the second copy of the specification. Where the
plugin and this disagree, one of them is wrong and a test should already have said so. It also means
what is missing is a statement: a route with no method here is a route the gate does not serve.

## Install

```shell
uv add pykeepassgate              # as a dependency of the project this is run in
uv tool install pykeepassgate     # or the command line on its own
```

`pip install pykeepassgate` takes the same path. A client works with a plugin of the same major
and minor version, as `0.1`, whatever the patch on either side. One dependency, `cryptography`, because the gate
hands over a PKCS#12 and Python's `ssl` reads PEM. Everything else, the command line included, is
the standard library.

## Pair

The operator presses **Invite** on the **KPG** tab of the database's settings dialog and sends
the invitation over a second channel. What they see while doing it is
[`docs/pairing.md`](https://github.com/mfrnd/KeePassGate/blob/master/docs/pairing.md), and worked
examples of everything below are
[`docs/pykeepassgate.md`](https://github.com/mfrnd/KeePassGate/blob/master/docs/pykeepassgate.md).
It is spent by the first client to use it and it does not survive KeePass closing.

```shell
pykeepassgate pair --name agent-invented --profile-name reader
```

The gate listens on 51776 unless its operator chose another port, and `pair` dials that without
being told. A different one goes in `--port`, and the **Listening** row of the operator's
**Tools**, **KeePassGate (KPG)** window shows which it is.

Leave `--invitation` off and the token is read from standard input, so it stays out of the shell
history and out of the process list. At a terminal it is asked for, and each character typed or
pasted shows as a `*`.

The same thing from a program:

```python
from pykeepassgate import api

credentials = api.pair(
    invitation="...",
    name="agent-invented",
    profiles=["reader"],
)

print(credentials.profiles)  # what the operator approved, not what was asked for
print(credentials.admission_key)  # what the operator's audit file names this certificate by
print(credentials.client_key)  # what the KPG tab knows this client by, kept through renewals
```

`pair` prints the profiles and the admission key, and `identity` prints them again with the
client key, without connecting.

## Use

```python
from pykeepassgate import GateClient

client = GateClient.open()
print(client.status())
print(client.identity())  # who the gate admits this client as, and which profiles count now
```

```shell
pykeepassgate status
pykeepassgate identity   # what is stored, without connecting
pykeepassgate whoami     # what the gate admits this client as now
```

**What a client may do at a node is something it can ask before trying.**
`client.entry_access(uuid)` and `client.group_access(uuid)` answer one mask per profile, as the
operator's tab shows a rule, and `.allows("RP")` asks whether one of those rules reads the
protected values there; `pykeepassgate access --entry <uuid>` prints them. A node where it may do
nothing is refused as one that is not there.

An entry KeePass shows as expired is listed and read like any other, because an expiry is advice
rather than a control. `client.entries(include_expired=False)` leaves the expired ones out, a read
entry's `expires` says when it expires, and `client.update_entry(uuid, expires=False)` takes the
mark off a password that has just been rotated.

**A value comes back as the entry stores it**, a field reference such as `{REF:P@I:...}`
included, and `pykeepassgate.refs.resolve(client, entry, value)` gives what KeePass would give
for it instead: it follows a reference by identifier, reading the entry it names under this client's
rules, and the placeholders for the entry's own strings, and refuses with `Unresolved` what it
cannot answer as KeePass would, such as a search by title. `pykeepassgate show <uuid> --resolve`
prints an entry that way.

**A write can wait on the node still holding what was read.** A group, an entry, a field or an
attachment read on its own carries `etag`, and every write about one takes `if_match=`: the write
is refused with `PreconditionFailed`, and nothing changed, when somebody changed the node after it
was read, the operator in KeePass among them. An entry's tag covers everything in it, and a write
answers with the tag it left, for the next write to name. The gate hands tags only to a client
whose rule reads everything a tag covers, and answers any other client's `if_match` with
`PreconditionFailed`.

**Nothing it hands back prints a value.** The `repr` of an entry, its fields, a field, an
attachment or a response names what it is and shows none of what it holds, so a log line or a
traceback carries no secret. A value is printed only by reading it.

**Every command takes `-o json`** for a program, such as `pykeepassgate entries -o json | jq`, and
`plain`, lines for a person, unless told otherwise. Both name a withheld value as withheld rather
than printing it empty, and a failure is a sentence on standard error and an exit code in either.

## Renew a certificate, or give one up

```shell
pykeepassgate renew --lifetime 30
pykeepassgate revoke
```

`renew` asks the operator for a new certificate in place of the one this client holds, which they
approve on screen as they did the pairing, so it waits while they read: six minutes, as pairing
does, or longer when `--connection-timeout` says so. It stores the new identity where the old one
was, by writing it whole beside the old one first and then writing the four files over, leaving
anything else in the directory alone, and then gives the old certificate up by presenting it.
`--lifetime` is a proposal, and the new `expires` is what the operator picked. When giving the old
one up does not happen, the renewal has still worked: the old identity is kept beside the new one
in a directory ending `.retired`, `renew` says so and exits 1, and `revoke --directory` on that
directory gives it up. A later `renew` gives it up first itself, and forgets it once the gate takes
it back, already refuses it or it has expired.

`revoke` asks the gate to revoke the certificate this client presents and, once the gate says the
revocation is saved, removes what is stored for it. When the gate does not confirm it,
nothing is removed. A certificate the gate already refuses, revoked or expired, counts as given up,
and its files are removed as well. From a program, `client.renew()` and `client.revoke_certificate()` ask the
gate, and `api.forget(directory)` removes the files.

## What to catch

Every exception says what to do next rather than what went wrong, because that is the part a caller
has to decide. Everything in the table but its last four rows derives from `GateError`, which
catches whatever this client raises of its own and nothing the standard library raises.

| Exception | What it means, and what to do |
| --- | --- |
| `DatabaseLocked` | The database is locked. Wait, and try again, or hand `retry_locked_timeout=` to the client and let it wait. Raised before anything is sent when the handshake was answered with the certificate only a locked database has, and as the gate's `503 locked` when the database is locked, or locks while a request is on its way: nothing was done, apart from a write whose handler had already run, which is `500 internal error`. |
| `GateBusy` | The gate cannot serve this right now: a write under a dialog open on what it would change, under a message box, or while KeePass is busy with something of its own for longer than a write waits, or a read while KeePass could not be held still for it. The same, with `retry_busy_timeout=`. |
| `AuthorityUndecided` | The database holds more than one certificate authority and the operator has not yet kept any of them, which KeePass asks about on its own screen. The same, with `retry_undecided_timeout=`, and pair again if the authority kept is not this client's. |
| `GateUnavailable` | The base of the three above, so catching it catches every `503`, and raised on its own for a `503` whose word this client does not know or that names none, with the word on `.error`. Nothing waits that one out. |
| `NotAdmitted` | The gate does not admit this certificate: it was never admitted, it was revoked, or it has expired, which is named as such, with the moment, before anything is dialed once this machine's clock is five minutes past it. To go on reaching the gate, pair again: a renewal needs a certificate that has not expired. To give the identity up, `revoke` removes what is stored for it. |
| `ProofRefused` | During pairing: the token is wrong, or whatever answered is not the KeePass whose operator holds it. Stop. |
| `PairingRefused` | The operator said no. The invitation is spent, so ask for another. |
| `PairingUnanswered` | Nobody answered the dialog. The invitation is **not** spent, so this can be tried again. |
| `PairingError` | The base of the three above, and raised on its own for the rest of what stops a pairing, with a message saying which. A name or a profile the gate would refuse, or more profiles than the 32 it takes, is caught before the request is sent: correct it and pair again. An invitation that is gone ran out or was spent: ask for another. An exchange the gate no longer holds leaves the invitation good: pair again with it. A pairing route the gate does not have is the two halves disagreeing about the wire. |
| `RenewalRefused` | The operator refused this renewal. On the dialog's own turn every refusal reads so, including one for this session, until a date or forever, which the next request is answered with at once, without the dialog. The certificate this client holds still works until it expires: ask again later, or pair again before then. |
| `RenewalRefusedThisSession` | The operator refused every renewal of this certificate until KeePass exits, and the gate keeps that answer in memory only. Ask again after KeePass has been restarted, or pair again before the one this client holds expires. |
| `RenewalTooEarly` | The operator refused renewing this certificate until `.not_before`, and asking before then is answered without anybody being asked. Ask again from then. |
| `RenewalRefusedForever` | The operator refused every renewal of this certificate. Pair again, with an invitation from the operator, before the one this client holds expires. |
| `RenewalUnanswered` | Nobody answered the renewal dialog, and nothing changed. Ask again when somebody is at the screen. |
| `RenewalError` | The base of the five above, for a caller that only wants to know whether the operator let the renewal happen. Never raised on its own. |
| `PreconditionFailed` | The gate did not serve a request made with `if_match`, so nothing was read or changed: `412`. Most often the node no longer holds what the tag named: somebody changed it after the tag was taken, another client, the operator in KeePass, a merge or a plugin. Read it again, which hands back its tag as it is now, and decide again. An `if_match` the gate cannot read, or any from a client the gate hands no tag to, is answered the same; send none. A `RouteRefused` with `status` `412`, so catching that catches this. |
| `RouteRefused` | Everything else the gate refuses, carrying the status and the reason it gave, usually one word: a `413` says in a sentence what was too large. A refused `head` carries the status alone, because its answer has no body to say anything in. A `404` is deliberately both "no such node" and "not yours", so a client cannot map a database by asking: if the node should be there, the rule is too narrow and the operator has to widen it. |
| `Withheld` | A protected value this client may not read, from every way of reading one: the rule that lets it read or list the entry does not grant `P`, which the operator can add. Never `None` or an empty string in its place; `entry.withheld` and `field.withheld` name what is kept back without a `try`, so `{name: entry.fields[name] for name in entry.fields if name not in entry.withheld}` reads every value this client may read. |
| `Unresolved` | From `pykeepassgate.refs.resolve` alone: a value it will not give as KeePass would, such as a reference that searches by title or names an entry this client cannot read, or a placeholder KeePass replaces and it does not. The message says which. |
| `MalformedToken` | The text handed over is not an invitation, so nothing was sent. Check what was pasted. |
| `ProtocolError` | Something on the wire is not what the protocol allows: an answer this client cannot read, or a request it will not send. Do not retry an answer it cannot read: the two halves disagree about the wire, which `tests/pykeepassgate/test_protocol.py` exists to stop. An answer longer than `max_response_bytes` is refused this way too, and the message says so: raise the setting rather than retry. |
| `NotPaired` | Nothing is stored for a paired client where this looked: nothing has paired under that profile, its identity was given up, or the directory named is not the one the pairing used. Pair, or name the profile or the directory it used. A `StorageError`, so catching that catches this as well. |
| `StorageError` | From `pair`, before anything is sent: the directory cannot hold credentials, and the message says why. Nothing is spent, so fix that and pair again. Afterwards: what is stored for this client will not read, or could not be written, and the message names the file or the directory to look at. A key stored under a password is one of these when no password was given, or the one given does not open it. |
| `TransportError` | No whole answer arrived: nothing is listening, the handshake failed, as below, or the connection broke or timed out once the request was on its way. After a read, send it again. After a write, the change may have landed, so look before sending it again: read or list what it changed, where the rules allow that, or ask the operator. |
| `ValueError` | A setting or an argument that could not work, refused where it is given and before anything is dialed, stored or spent: a port, a timeout, a retry budget, a response cap, a lifetime, a profile's name, an expiry, a key password or a `dial`. The message names it. |
| `AttributeError` | Something this client never read: a listed entry's `username`, `password`, `notes`, `expires` or `custom()`, a listed group's `notes`, a listed field's `value`, a listed group's or entry's `etag`. Read it first. Also the `etag` of a read the gate gave no tag for, because this client's rule does not read everything a tag covers, which the message says. Never raised for a withheld value. |
| `KeyError` | A name `entry.fields` or `entry.custom()` does not hold, as from any mapping. |
| `TypeError` | A `dial` that handed back something other than a connected socket. |

**The `503`s are the only refusals this client will wait out**, and only when a budget says so.
Each carries a `Retry-After` saying when to come back, and each of the three budgets, one per word,
says for how long to go on doing that. All three are off by default, so a refusal is raised exactly
where it happened, and a word none of them names is raised at once whatever they say. Nothing else
is retried, because waiting fixes none of it. `docs/` has the whole of it.

**A failed handshake says why.** `certificate has expired` is a database that has stayed locked
longer than ninety days, which unlocking it in KeePass mends.
[`docs/pykeepassgate.md`](https://github.com/mfrnd/KeePassGate/blob/master/docs/pykeepassgate.md)
gives the other reasons.

**An answer over 4 MiB fails whole with `ProtocolError`** rather than arriving cut short, and
asking again reads the same answer to the same cap. The plugin does not page, so a listing grows
with the database, and a very large one can need more: `max_response_bytes=` on the client, or
`--max-response-bytes` on the command line, raises it, and the message names both.

## Test a program built on it

`pykeepassgate.testing` is a scripted TLS stand-in for the gate: a certificate authority, a host
certificate under it, and a TLS 1.3 server on loopback that answers one request per connection, as
the plugin does, with whatever the test tells it to answer. It is not a model of the rules, so a
program tested against it has been tested against its script, and it imports no test framework.
[`docs/pykeepassgate.md`](https://github.com/mfrnd/KeePassGate/blob/master/docs/pykeepassgate.md)
shows a client pointed at one.

## What it does not do

**It does not open a `.kdbx`.** There is no file and no master password on this side of the API, by
design. What that means for anybody expecting a `pykeepass` object is measured on the
`spikes/pykeepass-compatibility` branch: 23 of its 56 public members are servable over the routes the
gate already has, and the object itself is not one of them.

**It defends nothing against local code execution as the user.** The client's key is on the disk,
because a client has to still be the same client tomorrow. The whole design concedes this
throughout, and so does this. The key can be stored under a password, with `pair --key-password`,
which asks at a terminal and takes the password no other way, or with `key_password=` from a
program, and that protects copies of the directory, such as backups and disk images, and nothing
against a process running as the user. How well it protects a copy is the password's to decide:
the key is encrypted with PBKDF2 at OpenSSL's default of 2048 iterations, which is little work per
guess, so a program should pass a long random one.
