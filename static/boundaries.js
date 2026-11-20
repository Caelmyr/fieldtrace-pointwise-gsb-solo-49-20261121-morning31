// Compose suggested trigger groups with the operator's boundary decisions.
// Pure logic, no DOM: unit-tested outside the browser.
//
// record: the /api/events/preview payload ({groups, boundaries}).
// decisions: one entry per record.boundaries — "time" (follow the gap rule),
//   "join" (bridge a between-group gap), "split" (break a within-group gap)
//   or "pending" (leave the touching photos unassigned for now).
// checked: indexes of the suggested groups selected for saving.
//
// Returns {groups, joins, held}: final frame-id groups to save, the confirmed
// join pairs to record, and the selected frame ids held back by pending
// boundaries.
export function composeEventGroups(record, decisions, checked) {
  const selected = new Set([...checked].map(Number));
  const order = [];
  record.groups.forEach((group, index) => {
    if (selected.has(index)) group.ids.forEach((id) => order.push({ id, group: index }));
  });
  const boundaryAt = {};
  (record.boundaries || []).forEach((boundary, index) => {
    boundaryAt[`${boundary.left}|${boundary.right}`] = { ...boundary, decision: decisions[index] || "time" };
  });
  const segments = [];
  const segmentOf = new Array(order.length);
  let current = [];
  order.forEach((item, i) => {
    if (i) {
      const previous = order[i - 1];
      const decision = boundaryAt[`${previous.id}|${item.id}`]?.decision || "time";
      let split = previous.group !== item.group;
      if (decision === "split") split = true;
      else if (decision === "join") split = false;
      if (split) {
        segments.push(current);
        current = [];
      }
    }
    segmentOf[i] = segments.length;
    current.push(i);
  });
  if (current.length) segments.push(current);
  const heldSegments = new Set();
  order.forEach((item, i) => {
    if (!i) return;
    const boundary = boundaryAt[`${order[i - 1].id}|${item.id}`];
    if (boundary?.decision === "pending") {
      heldSegments.add(segmentOf[i - 1]);
      heldSegments.add(segmentOf[i]);
    }
  });
  const groups = [], joins = [], held = [];
  segments.forEach((segment, index) => {
    if (heldSegments.has(index)) {
      held.push(...segment.map((i) => order[i].id));
      return;
    }
    groups.push(segment.map((i) => order[i].id));
    for (let k = 1; k < segment.length; k += 1) {
      const boundary = boundaryAt[`${order[segment[k - 1]].id}|${order[segment[k]].id}`];
      if (boundary?.decision === "join") joins.push({ left: boundary.left, right: boundary.right });
    }
  });
  return { groups, joins, held };
}
