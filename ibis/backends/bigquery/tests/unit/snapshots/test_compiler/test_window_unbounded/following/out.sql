SELECT sum(t0.`a`) OVER (ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS `tmp`
FROM t t0