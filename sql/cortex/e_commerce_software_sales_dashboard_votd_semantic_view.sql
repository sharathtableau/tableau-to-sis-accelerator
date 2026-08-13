-- Customers (DataDNA Dataset Challenge - E-commerce Dataset - November 2025): 'Select Date' has no physical column (PARAMETER_1) -- skipped.
CREATE OR REPLACE SEMANTIC VIEW WBR_DB.PUBLIC.E_COMMERCE_SOFTWARE_SALES_DASHBOARD_VOTD_SEMANTIC
  TABLES (
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025 AS TABLEAU_MIGRATION.PUBLIC.CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025
      WITH SYNONYMS = ('Customers (DataDNA Dataset Challenge - E-commerce Dataset - November 2025)')
      COMMENT = 'Migrated from Tableau datasource: Customers (DataDNA Dataset Challenge - E-commerce Dataset - November 2025)'
  )
  DIMENSIONS (
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.ACQUISITION_CHANNEL AS ACQUISITION_CHANNEL WITH SYNONYMS = ('Acquisition Channel'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.AGE_BAND AS AGE_BAND WITH SYNONYMS = ('Age Band'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.CATEGORY AS CATEGORY WITH SYNONYMS = ('Category'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.CHANNEL AS CHANNEL WITH SYNONYMS = ('Channel'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.COUNTRY AS COUNTRY WITH SYNONYMS = ('Country'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.CUSTOMER_ID AS CUSTOMER_ID WITH SYNONYMS = ('Customer Id'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.EVENT_DATE AS EVENT_DATE WITH SYNONYMS = ('Event Date'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.EVENT_TYPE AS EVENT_TYPE WITH SYNONYMS = ('Event Type'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.FIRST_RELEASE_DATE AS FIRST_RELEASE_DATE WITH SYNONYMS = ('First Release Date'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PAYMENT_METHOD AS PAYMENT_METHOD WITH SYNONYMS = ('Payment Method'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PRODUCT_NAME AS PRODUCT_NAME WITH SYNONYMS = ('Product Name'),
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.SIGNUP_DATE AS SIGNUP_DATE WITH SYNONYMS = ('Signup Date')
  )
  METRICS (
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.TOTAL_CUSTOMERS AS COUNT(DISTINCT CASE WHEN EVENT_TYPE = 'order' AND NOT IS_REFUNDED THEN CUSTOMER_ID END)
      WITH SYNONYMS = ('Total Customers', 'C.Loyal Customers (copy)_218706068024807424')
      COMMENT = 'Tableau calculated field: Total Customers',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PROD_REVENUE AS SUM(CASE WHEN NOT IS_REFUNDED THEN NET_REVENUE_USD END)
      WITH SYNONYMS = ('Prod. Revenue', 'CLV (copy)_1338976477651128320')
      COMMENT = 'Tableau calculated field: Prod. Revenue',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.METRIC_CALC AS CASE 1 WHEN 1 THEN (SUM(CASE WHEN NOT IS_REFUNDED THEN NET_REVENUE_USD END)) WHEN 2 THEN (SUM(CASE WHEN EVENT_TYPE = 'order' AND NOT IS_REFUNDED THEN QUANTITY END)) WHEN 3 THEN (COUNT(DISTINCT CASE WHEN NOT IS_REFUNDED THEN CUSTOMER_ID END)) END
      WITH SYNONYMS = ('Metric Calc.', 'Calculation_640637060630335488')
      COMMENT = 'Tableau calculated field: Metric Calc.',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PROD_CUSTOMERS AS COUNT(DISTINCT CASE WHEN NOT IS_REFUNDED THEN CUSTOMER_ID END)
      WITH SYNONYMS = ('Prod. Customers', 'Prod. Quantity (copy)_1338976477659062275')
      COMMENT = 'Tableau calculated field: Prod. Customers',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PROD_REFUND_RATE AS COUNT(DISTINCT CASE WHEN IS_REFUNDED THEN EVENT_ID END) / NULLIF(COUNT(DISTINCT CASE WHEN EVENT_TYPE = 'order' THEN EVENT_ID END), 0)
      WITH SYNONYMS = ('Prod. Refund Rate', 'Prod. Quantity (copy)_1338976477661065220')
      COMMENT = 'Tableau calculated field: Prod. Refund Rate',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.PROD_QUANTITY AS SUM(CASE WHEN EVENT_TYPE = 'order' AND NOT IS_REFUNDED THEN QUANTITY END)
      WITH SYNONYMS = ('Prod. Quantity', 'Prod. Revenue (copy)_1338976477658148866')
      COMMENT = 'Tableau calculated field: Prod. Quantity',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.MIN_0 AS MIN(0)
      WITH SYNONYMS = ('MIN(0)', 'Calculation_640637060768280581', 'Calculation_1840001932842614796', 'Calculation_1840001932843147280', 'Calculation_1803973140478283782')
      COMMENT = 'Tableau calculated field: MIN(0)',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.MIN_0_5 AS MIN(-0.5)
      WITH SYNONYMS = ('MIN(-0.5)', 'Calculation_1803973140450684931', 'Calculation_1803973140450947077')
      COMMENT = 'Tableau calculated field: MIN(-0.5)',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.AVG_0 AS AVG(0)
      WITH SYNONYMS = ('AVG(0)', 'Quantity', 'quantity', 'Customers', '__tableau_internal_object_id__].[Customers_45D1687D8E0B46B7B00BFCB78C3B6783', 'Calculation_640637060779524105', 'Calculation_1362057427997593606', 'Calculation_1362057427997638664')
      COMMENT = 'Tableau calculated field: AVG(0)',
    CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_NOVEMBER_2025.AVG_0_5 AS AVG(-0.5)
      WITH SYNONYMS = ('AVG(-0.5)', 'Calculation_640637060774944775')
      COMMENT = 'Tableau calculated field: AVG(-0.5)'
  )
  COMMENT = 'Generated by cortex_semantic.py from C:\Users\SHARAT~1\AppData\Local\Temp\twbconv_6sm0xyil\E-Commerce (Software) Sales Dashboard VOTD.twbx';
-- metric SKIPPED (window function -- not a scalar metric): Second Purchase Date
-- metric SKIPPED (unresolved token): P.AOV
-- metric SKIPPED (unresolved token): P.Days to Second Purchase
-- metric SKIPPED (unresolved token): C.Invoice Quantity.%
-- metric SKIPPED (unresolved token): C.Invoice Rev.%
-- metric SKIPPED (unresolved token): C.Order Quantity.%
-- metric SKIPPED (unresolved token): C.AOV
-- metric SKIPPED (unresolved token): P.Orders
-- metric SKIPPED (unresolved token): P.Quantity
-- metric SKIPPED (window function -- not a scalar metric): Repeat Purchase Rate
-- metric SKIPPED (unresolved token): P.Total Revenue
-- metric SKIPPED (unresolved token): C.Quantity
-- metric SKIPPED (unresolved token): CP. %Avg. Days to 2nd Purchase
-- metric SKIPPED (unresolved token): CP. %AOV
-- metric SKIPPED (unresolved token): CP. -%Total Revenue
-- metric SKIPPED (unresolved token): CP. %Quantity
-- metric SKIPPED (unresolved token): CP. %Total Revenue
-- metric SKIPPED (unresolved token): CP. +%Net
-- metric SKIPPED (unresolved token): CP. +%Gross
-- metric SKIPPED (unresolved token): CP. +%AOV
-- metric SKIPPED (unresolved token): CP. +%Avg. Days to 2nd Purch.
-- metric SKIPPED (unresolved token): CP. +%Quantity
-- metric SKIPPED (unresolved token): CP. -%Net
-- metric SKIPPED (unresolved token): CP. -%Gross
-- metric SKIPPED (unresolved token): CP. -%AOV
-- metric SKIPPED (unresolved token): CP. -%Avg. days to 2nd Purch.
-- metric SKIPPED (unresolved token): CP. +%Total Revenue
-- metric SKIPPED (unresolved token): CP. -%Quantity
-- metric SKIPPED (unresolved token): Current Period
-- metric SKIPPED (unresolved token): C.Total Revenue
-- metric SKIPPED (unresolved token): C.Order Rev.%
-- metric SKIPPED (window function -- not a scalar metric): Customer Total Revenue
-- metric SKIPPED (unresolved token): Max. Total Revenue
-- metric SKIPPED (window function -- not a scalar metric): Customer Total Order
-- metric SKIPPED (unresolved token): C.Net Revenue 
-- metric SKIPPED (unresolved token): C.Orders
-- metric SKIPPED (window function -- not a scalar metric):  First Purchase Date
-- metric SKIPPED (unresolved token): C.Days to Second Purchase
-- metric SKIPPED (unresolved token): Prior Period
-- metric SKIPPED (window function -- not a scalar metric): Is Loyal Customer
-- metric SKIPPED (window function -- not a scalar metric): Loyal Customers
-- metric SKIPPED (window function -- not a scalar metric): CLV
-- metric SKIPPED (window function -- not a scalar metric): Avg. Days to Second Purchase
-- metric SKIPPED (unresolved token): P.Gross Revenue
-- metric SKIPPED (unresolved token): Max. AOV
-- metric SKIPPED (window function -- not a scalar metric): Max. Avg. Days to Second Purchase
-- metric SKIPPED (unresolved token): Max. Quantity
-- metric SKIPPED (unresolved token): P.Net Revenue
-- metric SKIPPED (unresolved token): C.Gross Revenue
-- metric SKIPPED (unresolved token): SIGN([CP. %AOV])
-- metric SKIPPED (unresolved token): SIGN([CP. %Avg. Days to 2nd Purchase])
-- metric SKIPPED (unresolved token): SIGN([CP. %Quantity])
-- metric SKIPPED (unresolved token): SIGN([CP. %Total Revenue])
-- metric SKIPPED (window function -- not a scalar metric):  First Purchase Date (copy)_808114670314786817
-- metric SKIPPED (unresolved token): C.AOV (copy)_350999307064262665
-- metric SKIPPED (unresolved token): C.Days to Second Purchase (copy)_808114670321188869
-- metric SKIPPED (unresolved token): C.Invoice Rev.% (copy)_1840001932883554336
-- metric SKIPPED (unresolved token): C.Order % (copy)_1213157159460585483
-- metric SKIPPED (unresolved token): C.Order Rev.% (copy)_1840001932883726369
-- metric SKIPPED (unresolved token): C.Orders (copy)_350999307064033288
-- metric SKIPPED (unresolved token): C.Orders (copy)_787566995935674369
-- metric SKIPPED (unresolved token): C.Quantity (copy)_1840001932867850266
-- metric SKIPPED (window function -- not a scalar metric): C.Total Customers (copy)_218706068025290754
-- metric SKIPPED (unresolved token): C.Total Revenue (copy)_1213157159442825224
-- metric SKIPPED (unresolved token): C.Total Revenue (copy)_1840001932864798745
-- metric SKIPPED (unresolved token): CP. %AOV (copy)_197313975166783490
-- metric SKIPPED (unresolved token): CP. %Quantity (copy)_197313975164694528
-- metric SKIPPED (unresolved token): CP. %Total Revenue (copy 2)_1840001932852453396
-- metric SKIPPED (unresolved token): CP. %Total Revenue (copy) (copy)_1840001932868198427
-- metric SKIPPED (unresolved token): CP. %Total Revenue (copy)_1840001932847951891
-- metric SKIPPED (unresolved token): CP. +%Gross (copy)_350999307061702663
-- metric SKIPPED (unresolved token): CP. +%Orders (copy)_350999307061432325
-- metric SKIPPED (unresolved token): CP. +%Quantity (copy)_350999307058647041
-- metric SKIPPED (unresolved token): CP. +%Quantity (copy)_808114670321713159
-- metric SKIPPED (unresolved token): CP. +%Total Revenue (copy) (copy)_1840001932869410845
-- metric SKIPPED (unresolved token): CP. -%Gross (copy)_350999307061563398
-- metric SKIPPED (unresolved token): CP. -%Orders (copy)_350999307060899842
-- metric SKIPPED (unresolved token): CP. -%Quantity (copy)_350999307058434048
-- metric SKIPPED (unresolved token): CP. -%Quantity (copy)_808114670321446918
-- metric SKIPPED (unresolved token): CP. -%Total Revenue (copy)_1840001932853108757
-- metric SKIPPED (unresolved token): CP. -%Total Revenue (copy)_1840001932868481052
-- metric SKIPPED (unresolved token): Calculation_1213157159435526148
-- metric SKIPPED (unresolved token): Calculation_1213157159441965062
-- metric SKIPPED (unresolved token): Calculation_1213157159458582538
-- metric SKIPPED (window function -- not a scalar metric): Calculation_1672524324870516736
-- metric SKIPPED (unresolved token): Calculation_1840001932833705988
-- metric SKIPPED (window function -- not a scalar metric): Calculation_350999307073978380
-- metric SKIPPED (unresolved token): Calculation_436004750011006976
-- metric SKIPPED (unresolved token): Calculation_787566995935305728
-- metric SKIPPED (window function -- not a scalar metric): Calculation_808114670314459136
-- metric SKIPPED (unresolved token): Calculation_808114670315163650
-- metric SKIPPED (unresolved token): Current Period (copy)_1213157159435792389
-- metric SKIPPED (window function -- not a scalar metric): Customer Total Order (copy)_350999307074154509
-- metric SKIPPED (window function -- not a scalar metric): Customer Total Order (copy)_350999307074367502
-- metric SKIPPED (window function -- not a scalar metric): Customer Total Revenue (copy)_1672524324871475201
-- metric SKIPPED (window function -- not a scalar metric): Days to Second Purchase (copy)_808114670319501315
-- metric SKIPPED (unresolved token): Gross Revenue (copy)_350999307061026819
-- metric SKIPPED (unresolved token): Max. Quantity (copy)_350999307066716170
-- metric SKIPPED (window function -- not a scalar metric): Max. Quantity (copy)_808114670319947780
-- metric SKIPPED (unresolved token): Max. Total Revenue (copy)_1840001932874211358
-- metric SKIPPED (unresolved token): Net Revenue  (copy)_350999307061145604
-- metric SKIPPED (unresolved token): Net Revenue  (copy)_436004750011314177
-- metric SKIPPED (unresolved token): Calculation_197313975164899329
-- metric SKIPPED (unresolved token): Calculation_197313975167160323
-- metric SKIPPED (unresolved token): Calculation_1840001932880830495
-- metric SKIPPED (unresolved token): Calculation_1840001932853657622
-- metric SKIPPED (unresolved token): Calculation_1840001932853817368
