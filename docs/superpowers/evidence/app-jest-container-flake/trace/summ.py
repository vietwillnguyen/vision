# Summarise a trace log: Babel transform time that landed inside a running test.
# usage: python3 summ.py <TRACE_OUT>
import collections,sys
lines=[l.rstrip('\n').split(' ',3) for l in open(sys.argv[1])]
cur={}; inside=collections.Counter(); n=0
for t,d,pid,f in lines:
  if f.startswith('TEST_START'): cur[pid]=f[11:]
  elif f.startswith('TEST_END'): cur.pop(pid,None)
  elif pid in cur: inside[cur[pid]]+=int(d); n+=1
print(f'modules transformed inside a running test: {n}, {sum(inside.values())} ms of Babel CPU')
for k,v in inside.most_common(8): print(f'  {v:6d} ms  {k}')
