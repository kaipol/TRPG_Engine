import sqlite3

from ..common.output import get_output

_db_file = ""

GREAT_SF_RULE_DEFAULT = 2
GREAT_SF_RULE_STR = [
    "",
    get_output("coc_rule.rule_1"),
    get_output("coc_rule.rule_2"),
    get_output("coc_rule.rule_3"),
    get_output("coc_rule.rule_4"),
]

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#  COC great success/failure rule.                                          #
#  -- 1: strict rule.                                                       #
#        1 => great success, 100 => great failure                           #
#  -- 2: official COC7th rule (default, recommended).                       #
#        for skills < 50:                                                   #
#            1 => great success, 96~100 => great failure                    #
#        for skills >= 50:                                                  #
#            1 => great success, 96~100 => great failure                    #
#  -- 3: phased rule (recommended).                                         #
#        for skills < 50:                                                   #
#            1 => great success, 96~100 => great failure                    #
#        for skills >= 50:                                                  #
#            1~5 => great success, 100 => great failure                     #
#  -- 4: loose rule.                                                        #
#        1~min(5, skill level) => great success, 96~100 => great failure    #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
GLOBAL_SET = True


def configure_coc_rules(db_file: str):
    global _db_file
    _db_file = db_file


def _connect_rules_db():
    if not _db_file:
        raise RuntimeError("COC rules database is not configured")
    return sqlite3.connect(_db_file, timeout=10)


def coc_rule_init():
    '''
    Create table for COC rule settings in the host engine database.
    Args:
        None.
    Returns:
        None.
    '''
    ruledb = _connect_rules_db()
    csr = ruledb.cursor()
    csr.execute(
        """
        CREATE TABLE IF NOT EXISTS dice_coc_rules (
            group_id TEXT PRIMARY KEY,
            rule INTEGER NOT NULL
        );
        """
    )
    ruledb.commit()
    ruledb.close()

def fetch_group_rule(group:str)->int:
    '''
    Ask rule # in given group.
    Args:
        group(str): QQ group id.
    Returns:
        int: rule id, -1 for group not exist.
    '''
    coc_rule_init()
    ruledb = _connect_rules_db()
    try:
        csr = ruledb.cursor()
        csr.execute("SELECT rule FROM dice_coc_rules WHERE group_id = ?", (str(group),))
        res = csr.fetchone()
        return int(res[0]) if res is not None else -1
    finally:
        ruledb.close()

def great_success_range(skill_level:int, rule:int)->list:
    '''
    Ask for range of great success in current rule.
    Args:
        skill_level(int): Skill level of ra check.
    Returns:
        list: The range of great success (if first member is pos), or error info (if neg). 
    '''

    def min(a:int, b:int)->int: return a if a < b else b

    res = []
        
    # get result
    if   rule == 1 or rule == 2:
        res = range(1, 1+1)
    elif rule == 3:
        res = range(1, 1+1) if skill_level < 50 else range(1, 5+1)
    elif rule == 4:
        res = range(1, min(5, skill_level)+1)
    else:
        res = [-2, "InvalidRuleNum"]
    
    return res

def great_failure_range(skill_level:int, rule:int)->list:
    '''
    Ask for range of great failure in current rule.
    Args:
        skill_level(int): Skill level of ra check.
    Returns:
        list: The range of great failure (if first member is pos), or error info (if neg). 
    '''

    res = []
        
    # get result
    if   rule == 1:
        res = range(100, 100+1)
    elif rule == 2 or rule == 3:
        res = range(96, 100+1) if skill_level < 50 else range(100, 100+1)
    elif rule == 4:
        res = range(96, 100+1)
    else:
        res = [-2, "InvalidRuleNum"]
    
    return res

def set_great_sf_rule(rule:int, group:str)->int:
    '''
    Change rule # in given group.
    Args:
        group(str): QQ group id.
        rule(int): rule id.
    Returns:
        int: 1 for succeed, neg number for error. 
    '''

    coc_rule_init()
    ruledb = _connect_rules_db()
    csr = ruledb.cursor()

    if rule < 1 or rule > 4:
        rule = GREAT_SF_RULE_DEFAULT

    csr.execute(
        """
        INSERT INTO dice_coc_rules (group_id, rule)
        VALUES (?, ?)
        ON CONFLICT(group_id) DO UPDATE SET rule = excluded.rule
        """,
        (str(group), int(rule)),
    )
    ruledb.commit()
    ruledb.close()
    return 1    # Exec Succeed

def get_great_sf_rule(group: str) -> int:
    '''
    获取群组规则，如果群组不存在则自动创建并返回默认规则。
    '''
    coc_rule_init()
    group_id_str = str(group)
    ruledb = _connect_rules_db()
    csr = ruledb.cursor()

    try:
        csr.execute("SELECT rule FROM dice_coc_rules WHERE group_id = ?", (group_id_str,))
        res = csr.fetchone()

        if res is not None:
            rule_id = int(res[0])
        else:
            rule_id = GREAT_SF_RULE_DEFAULT
            csr.execute("INSERT INTO dice_coc_rules (group_id, rule) VALUES (?, ?)", (group_id_str, rule_id))
            ruledb.commit()

        return rule_id
    finally:
        ruledb.close()


def modify_coc_great_sf_rule_command(group_id, command: str = " "):
    """
    Check or Modify current great success/failure rule.
    """
    coc_rule_init()

    # check command
    rule_set = 0
    if command[0] == "1":
        rule_set = 1
    elif command[0] == "2":
        rule_set = 2
    elif command[0] == "3":
        rule_set = 3
    elif command[0] == "4":
        rule_set = 4
    elif command[0] == "0":
        rule_set = GREAT_SF_RULE_DEFAULT
    else:
        rule_set = -1

    # set rule
    if rule_set > 0:
        sgsfr_res = set_great_sf_rule(rule_set, str(group_id))
        return get_output("coc_roll.set_rule", rule=GREAT_SF_RULE_STR[rule_set])
    # plain help
    else:
        res_str = get_output(
            "coc_rule.help",
            rule_1=GREAT_SF_RULE_STR[1],
            rule_2=GREAT_SF_RULE_STR[2],
            rule_3=GREAT_SF_RULE_STR[3],
            rule_4=GREAT_SF_RULE_STR[4],
            default_rule=GREAT_SF_RULE_STR[GREAT_SF_RULE_DEFAULT],
            current_rule=GREAT_SF_RULE_STR[int(get_great_sf_rule(group_id))],
        )
        return res_str
