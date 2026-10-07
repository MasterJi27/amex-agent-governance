package governance.authz

default allow = false

agents := {
  "travel-concierge": ["rebook_flight", "change_hotel", "send_notification"],
  "claims-assistant": ["rebook_flight", "change_hotel", "send_notification"],
  "refund-agent": ["send_notification"],
}

allow {
  some i
  agents[input.agent_id][i] == input.action
}
