# Security setup before hosting

Set a unique random `HBL_API_TOKEN` in the deployment secret store. Administrative API calls, including documentation, template operations, issuance and OAuth initiation, require `Authorization: Bearer <token>`. An empty token disables these routes. Keep the API on loopback or behind HTTPS. Health and OAuth callback routes remain public; callbacks require a matching, unexpired, single-use browser binding.

To connect ClickUp, open the local API root in the browser that will authorize ClickUp. Run this in that page's browser console; the token is entered at a prompt rather than stored in a URL:

```javascript
const response = await fetch('/auth/clickup/start?redirect=false', {
  headers: {Authorization: 'Bearer ' + prompt('HBL API token')}
});
if (!response.ok) throw new Error('OAuth initiation failed');
location.assign((await response.json()).authorization_url);
```

The default redirect response remains available to authenticated clients that preserve the browser cookie. Set `APP_BASE_URL` to the external HTTPS origin when hosting so the binding cookie is Secure. OAuth state is in process memory; restart or a different worker rejects the callback. Use one worker until shared state storage is configured.

Canonical JSON may carry business data. Favorable QA verdicts and submitted warnings are discarded; unresolved hard blockers are retained in addition to recalculated server QA. Only installed approved configuration can restore a total-only package exception. The service uses the live ClickUp HBL and Owner Country and recalculates QA before selecting issuance. Existing approval fields remain required. This does not establish that approval covers an unchanged content version; a content-bound approval workflow remains separate work.

Shipment identifiers must be safe filename components. Unsafe identities are rejected instead of rewritten. Spreadsheet source text is written as literal cells; intentional template formulas remain formulas.

## AWS deployment roles

Use separately authorized administrator credentials to run `bash aws/verification-service/scripts/bootstrap_iam.sh` once for the chosen environment. This creates the named Lambda execution role with logging and table GetItem permissions. Review existing role policies independently; bootstrap does not remove unrelated policies already attached to a role.

Use a restricted deployment identity with the parameterized `deploy-iam-policy.json` for `bash aws/verification-service/scripts/deploy_aws_cli.sh`. This identity can read and pass the existing role only to Lambda; it cannot create the role or attach/write role policies. Deployment fails before resource writes if the role is missing. The SAM deployment remains a separate administrator provisioning path.

These source changes do not update an already-installed IAM policy or deploy any resources. Verify the actual deployment identity, execution role, boundaries and network controls before migration. The public verification Lambda remains read-only and separate from the administrative API.

Approved total-only package exceptions are rebuilt from installed customer YAML only when Guatemala, the HBL number, total packages, and complete container identity list match the approved example. Submitted QA warnings cannot create an exception. Other learned customer fields are not rewritten during this check.
