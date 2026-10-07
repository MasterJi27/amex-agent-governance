local amount = tonumber(ARGV[1])
local n = #KEYS
for i = 1, n do
  local spent = tonumber(redis.call("GET", KEYS[i]) or "0")
  local next_spent = spent - amount
  if next_spent < 0 then
    next_spent = 0
  end
  redis.call("SET", KEYS[i], next_spent)
end
return 1
