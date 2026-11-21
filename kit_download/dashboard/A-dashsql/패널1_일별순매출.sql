select DT, sum(NET_AMT) as NET_AMT from NX_AGG1_DSL where DT BETWEEN '20261001' AND '20261031' group by DT order by DT
