# Renewal may select a different package

Owner decision, 2026-10-19: a customer may renew one existing service with
any package available to that customer, including changing quota and the
per-Purchase session cap. For example, an unlimited, single-user service
may renew with a 20 GiB, two-user package.

`renew_purchase_id` identifies the service being renewed. The approval now
records that service's ID and its original `package_id` separately from
`package_id_snapshot`, which identifies the package being purchased. T1
compares the live service to the original snapshot; the existing manifest
continues to bind the purchased package and its effects. Usage updates do
not invalidate the target. Changing ownership, deleting the service, or
changing its original package after registration still refuses execution.

The source snapshot columns are additive and nullable. Existing approvals
are not backfilled from today's state: their previous same-package check
is retained. A newly registered approval also supports a source service
whose original package is NULL. No frozen design document is rewritten.

The current renewal timing is retained: a usable service queues the new
package until its current quota or expiry ends; an exhausted service
applies it immediately. Existing connections are retained. Quota zero in
a purchased package means unlimited quota, and duration zero means no
expiry; these values replace the old limits when the renewal activates.
Manual additions without a package retain their existing zero-means-no-
change behavior. Finite renewals in the single existing reservation slot
continue to accumulate; an unlimited package in that slot keeps that
dimension unlimited.

Receipt #1031 was already cancelled and its bot request rejected before
this correction. This change does not reopen it or create a sale. Recovery
must use an authenticated supported request and verify the original
payment has not funded another sale.
