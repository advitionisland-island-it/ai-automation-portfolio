#!/bin/sh
# Import this directory's workflows and the Mailpit SMTP credential, publish the workflows, then
# start n8n. The files here are the source of truth: every start overwrites what n8n has, so
# nothing has to be clicked in the UI (AC-4.1). The CLI writes to n8n's database directly, which
# is why this runs before `n8n start` and not while n8n is running.
set -eu
n8n import:credentials --input=/opt/p01-n8n/credentials/mailpit-smtp.json
n8n import:workflow --separate --input=/opt/p01-n8n/workflows
# An error workflow (W3) only runs when it is published, like the others.
for id in P01W1InboundLead P01W2ReviewMail0 P01W3AlertMail00; do
  n8n publish:workflow --id="$id"
done
exec n8n start
