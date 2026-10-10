module a {
  var x: int
  action init = x' = 0
  action inc = x' = x + 1
  action stay = x' = x
  action step = inc
}
