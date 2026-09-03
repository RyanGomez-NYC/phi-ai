# Extending a connector yourself

Nine of the fifteen EMRs this platform connects to will not accept a write
over their certified FHIR API. That is not a gap in this software and it is
not something a code change can fix: their published API genuinely does not
offer it. What most of those vendors *do* offer is a second, separately sold
product that does — and that is what this guide is about.

Read this if a delivery is refusing and you want to know whether it can ever
succeed, what you would have to buy, and what you would have to write.

---

## 1. First, find out what you are actually dealing with

Ask the platform:

```python
from core.fhir.commercial import explain
print(explain("altera"))
```

You get one of three answers.

**"No commercial write path is documented by this vendor."**
TruBridge, Practice Fusion, MEDITECH and Netsmart are here. Their certified
FHIR API is the entire published surface. There is nothing to buy and nothing
to write; a delivery they refuse has nowhere else to go. If you have a
contract that says otherwise, that is new information — see §5.

**A refusal naming a product.** The vendor sells a write path and this
deployment has not configured it. The message names the product, how it is
obtained, and who to contact.

**Nothing refuses.** The certified API accepts the write and you do not need
any of this.

---

## 2. Understand which of the three kinds you have

The nine are not the same problem, and the difference decides how much work
you are signing up for.

### It is still FHIR, just switched on
**eClinicalWorks, MEDHOST, Epic, Nextech (partly).**

The endpoints, the auth and the resources are the ones you already speak.
What is missing is a contract, a licence or a scope.

- **eClinicalWorks** documents Create/Update from V12.0.2 as a contracted
  add-on, arranged through `interop@eclinicalworks.com`.
- **MEDHOST** is a licence the *facility* buys — "customers must purchase and
  activate the MEDHOST Interoperability package" — not something you can buy
  for them.
- **Epic** enables write APIs per health system, per resource, per flavour. The
  same Epic that refuses at one hospital may accept at another.
- **Nextech** publishes a narrow write surface that differs by product:
  Select/NexCloud documents DocumentReference create only; Practice+ on STU3
  adds Patient, Appointment and PaymentReconciliation.

**Try the existing writer first.** Once the scopes are granted,
`core/fhir/delivery/writer.py` may simply work: it asks the destination's own
CapabilityStatement what it will accept, so a newly-enabled create appears
there without any code change. Do that before writing a connector — you may
not need one.

### It is a different API entirely
**Altera and Veradigm (Unity), Greenway (GAPI), ModMed (proprietary), NextGen
(Enterprise).**

These are not FHIR. Different protocol, different auth, different data shapes,
usually a different base URL and portal. Writing one is writing a client for
somebody else's API — days to weeks, not an afternoon — and the FHIR writer
will not help you.

Two traps worth knowing before you start:

- **Greenway's GAPI** is, in their own words, "a Proprietary API with separate
  and distinct API calls and data structures for each of our EHR products".
  Intergy and Prime Suite differ. That is realistically two implementations.
- **ModMed's proprietary API** covers EMA and Practice Management only. ModMed
  states it "will not be able to support gGastro customers", so a gGastro
  practice has no write path even with the contract.

### It is a commercial gate, not an API at all
**Epic and MEDHOST**, again, belong here as much as above: nothing technical
is missing. Somebody has to sign something.

---

## 3. Buy or sign the thing

This step is not engineering and it is usually the long pole. Each stub in
`core/fhir/commercial/vendors.py` carries the vendor's own terms and a contact
in its docstring — `how_to_obtain` and `contact` on the class, and `sources`
naming where each claim came from so you can check it yourself.

Expect: a developer-programme membership or marketplace listing, a technical
review before production access, per-customer activation by the practice, and
in several cases a fee. ModMed's review is explicit — a vendor must pass it
"before they are permitted to gain access to their first customer's production
system".

---

## 4. Write the connector

Everything lives in `core/fhir/commercial/`. You are filling in three methods
on a class that already exists.

```python
# core/fhir/commercial/vendors.py
class AlteraUnity(CommercialConnector):
    vendor_key = "altera"
    product = "Unity API (proprietary, bidirectional)"
    ...

    def authenticate(self) -> None:
        # Obtain whatever Unity uses. Not necessarily OAuth.
        # Store the result on self; raise ConnectorNotConfigured if the
        # deployment has no credentials.

    def capabilities(self) -> CommercialCapability:
        # Ask the live endpoint what it will accept, and return it.
        # Do not return the sales material. Set verified_at when a human
        # has confirmed it.

    def create(self, resource_type, resource, *, dry_run=True) -> CommercialWrite:
        # Translate the FHIR resource into the vendor's shape and write it.
        # Honour dry_run: compose, audit, and do not send.
```

Then set `available()` to return True when the deployment is configured, and
the delivery path can offer the route.

**The rules that are not negotiable**, because they are what makes a refusal
trustworthy:

1. **Never return a plausible empty success.** If you cannot write, raise
   `CommercialWriteRefused` with a reason a person can act on. A delivery that
   silently went nowhere is worse than one that failed loudly — nobody goes
   looking for it.
2. **`dry_run=True` is the default and must genuinely not write.** Compose the
   request, record the audit event, send nothing.
3. **`capabilities()` reports the endpoint, not the brochure.** What a
   customer's build has enabled is theirs to configure, and only their server
   knows it — the same reason the FHIR writer asks for a CapabilityStatement
   instead of trusting the profile table.
4. **Audit every write the same way the FHIR path does**, before it is sent.
5. **Do not weaken the governance path to make a write succeed.** Consent,
   segmentation and role checks run before a connector is ever consulted, and
   a commercial API does not earn an exemption from them.

---

## 5. Tell the platform what you learned

If your contract reveals something this repository has wrong or does not know
— a base URL, an auth scheme, a resource that is writable after all — update
the vendor's profile in `core/fhir/emr_profiles.py` and its chapter in
`docs/EMR_CONNECTORS.md`, and cite the document you learned it from.

The rule this project holds to: **each vendor's own documentation is the source
of truth for that vendor.** Not another vendor's behaviour, not an inference
from how Epic works, and not a hopeful default. If you cannot cite it, the
profile should keep refusing and say why.

---

## Getting help

If you want help working out which add-on a system needs, or wiring one once
it is licensed: **phi-ai@ryangomez.nyc**.
