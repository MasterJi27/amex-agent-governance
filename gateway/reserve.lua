local amount = tonumber(ARGV[1])
local reasons = {}
for reason in string.gmatch(ARGV[2], "[^,]+") do
  table.insert(reasons, reason)
end
local n = #reasons
for i = 1, n do
  local cap = tonumber(redis.call("GET", KEYS[(i - 1) * 2 + 1]) or "0")
  local spent = tonumber(redis.call("GET", KEYS[(i - 1) * 2 + 2]) or "0")
  if spent + amount > cap then
    return {0, reasons[i], cap - spent}
  end
end
local remaining = 0
for i = 1, n do
  local cap = tonumber(redis.call("GET", KEYS[(i - 1) * 2 + 1]) or "0")
  local spent = tonumber(redis.call("GET", KEYS[(i - 1) * 2 + 2]) or "0")
  local next_spent = spent + amount
  redis.call("SET", KEYS[(i - 1) * 2 + 2], next_spent)
  if i == n then
    remaining = cap - next_spent
  end
end
return {1, "OK", remaining}
