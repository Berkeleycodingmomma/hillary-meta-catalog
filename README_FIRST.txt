HILLARY META CATALOG — REGISTRY UPGRADE
========================================

WHAT THIS UPGRADE DOES
----------------------
1. Scans Hillary's public storefront and linked Idea List pages for accessible lists.
2. Remembers lists by permanent Amazon list ID, even when Hillary renames them.
3. Keeps the current 30 lists approved for Meta.
4. Adds newly discovered lists to config/idea_list_registry.csv as include_in_meta=no.
5. Detects renamed lists.
6. Detects product changes in unapproved lists when those lists can be discovered and opened.
7. Creates reports for new, renamed, and changed-unapproved lists.
8. Uses stable Meta labels such as IL_2Q5678F6BP61R so Meta Product Sets survive Amazon title changes.
9. Creates reports/meta_product_set_guide.csv with the exact set name and five OR rules.
10. Keeps every product's readable memberships in output/product_memberships.csv.

IMPORTANT LIMITATION
--------------------
The program can only discover Idea Lists that Amazon exposes publicly through Hillary's storefront or pages linked from it. A private, hidden, or completely unlinked old list cannot be detected automatically until Amazon makes it publicly accessible again or its URL is added to the registry.

INSTALLATION — REPLACE FOUR FILES
---------------------------------
In the CORRECT GitHub Desktop repository folder, replace:

  amazon_meta_sync.py
  config/approved_lists.csv
  config/settings.json

Then ADD:

  config/idea_list_registry.csv

Do not replace run.command, setup.command, requirements.txt, or the public folder.

FIRST RUN
---------
1. Keep auto_git_publish=false for the first test.
2. Run run.command from the correct GitHub Desktop folder.
3. Enter the Partner Tag, Credential ID, and Secret.
4. The first discovery run may be much longer because it can inspect many old lists.
5. Review:
     reports/latest_run_report.txt
     reports/new_lists_found.csv
     reports/renamed_lists.csv
     reports/changed_unapproved_lists.csv
     reports/meta_product_set_guide.csv
     config/idea_list_registry.csv

HOW TO APPROVE A LIST
---------------------
Open config/idea_list_registry.csv and change include_in_meta from no to yes for that list. Save it and run run.command again.

META PRODUCT SETS
-----------------
The new feed uses stable labels, not the changing Amazon title. Example:

  IL_2Q5678F6BP61R

Open reports/meta_product_set_guide.csv. For each set, create one Meta Product Set with OR rules across custom_label_0 through custom_label_4 using the stable label shown in the guide.

Because these are stable list-ID labels, Hillary can rename June Bestsellers to July Bestsellers without breaking the product-set membership rule. You may rename the visible Meta set, but the rule stays the same.

AFTER THE FIRST SUCCESSFUL TEST
-------------------------------
Change auto_git_publish to true in config/settings.json. Then each successful run can commit and push the public feed and reports automatically.

The registry is local by default. publish_registry_to_git is false because your GitHub repository is public. Leave it false unless you intentionally want the registry published.
