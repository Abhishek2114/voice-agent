# Sample workspace: ShrotiHost (demonstration)

An optional, importable workspace for trying voice-agent with a realistic web-hosting business.

**Everything here is demonstration content.**

* The business profile, knowledge base and agent instructions are examples written for this repository. They are not ShrotiHost's official material.
* **No prices are included as facts.** The pricing document contains clearly marked placeholders. Replace it with your real, current pricing before talking to real customers. The agent is instructed never to quote a price that isn't in its knowledge.
* All contacts are fictional. Their phone numbers use the `+91 99999 0xxxx` range, chosen as obviously fake test data, and every contact is flagged as a sample (`is_fictional`).
* The sample campaign is a draft in **SIMULATION** mode. Imports can never create real-calling campaigns.
* The consent records on the fictional contacts exist only so the sample campaign can demonstrate the compliance checks. They are not real consent.

## Import

1. Dashboard: **Settings → New workspace → Import a workspace**, then choose `demo/shrotihost/workspace.json`.
2. Or with the API (when signed in):
   ```bash
   curl -b cookies.txt -H "x-csrf-token: $CSRF" -F file=@demo/shrotihost/workspace.json \
     http://localhost:3000/api/workspaces/import
   ```

The import creates a new workspace named "ShrotiHost (sample)" with:

* 2 agents: **Shruti**, an inbound support and sales assistant that captures leads, and **Aarav**, an outbound follow-up caller
* 6 knowledge entries: FAQ, plans overview, a pricing placeholder, support policy, refund policy, migration service
* 8 fictional contacts tagged `sample` (4 tagged `festive-offer`)
* 1 draft simulation campaign: "Festive offer follow-up (simulation)"

`contacts-sample.csv` is the same fictional contact list as a CSV, for trying the import wizard.
