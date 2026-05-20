from . import controllers, models, services, wizard


def _post_init_migrate_invoice_type_code(env):
    """
    Migrations for tca_invoice_type_code:
      1. tca_is_out_of_scope=TRUE → '480' / '81'
      2. tca_is_self_billing=TRUE → '380_sb' / '381_sb'
         (must run before tca_is_self_billing recomputes from the new
         computed-stored definition, which would wipe the legacy values)
    """
    # ── 1. Out-of-scope migration ─────────────────────────────────────────
    env.cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'account_move' AND column_name = 'tca_is_out_of_scope'
    """)
    if env.cr.fetchone():
        env.cr.execute("""
            UPDATE account_move
            SET tca_invoice_type_code = CASE
                WHEN move_type IN ('out_refund', 'in_refund') THEN '81'
                ELSE '480'
            END
            WHERE tca_is_out_of_scope = TRUE
              AND (tca_invoice_type_code IS NULL OR tca_invoice_type_code = '')
        """)

    # ── 2. Self-billing migration ─────────────────────────────────────────
    # Existing records with tca_is_self_billing=TRUE need their
    # tca_invoice_type_code switched to the _sb variant. OOS codes (480/81)
    # have no self-billing variant in PINT AE, so they're left untouched.
    env.cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'account_move' AND column_name = 'tca_is_self_billing'
    """)
    if env.cr.fetchone():
        env.cr.execute("""
            UPDATE account_move
            SET tca_invoice_type_code = CASE
                WHEN tca_invoice_type_code = '380' THEN '380_sb'
                WHEN tca_invoice_type_code = '381' THEN '381_sb'
                ELSE tca_invoice_type_code
            END
            WHERE tca_is_self_billing = TRUE
              AND tca_invoice_type_code IN ('380', '381')
        """)

    # ── 3. Align tca_invoice_type_code with move_type direction ───────────
    # Existing records (especially credit notes created via reversal before
    # the create() override was added) may have a type code that doesn't match
    # their move_type — e.g. an out_refund stuck at '380'. Flip them now.
    env.cr.execute("""
        UPDATE account_move SET tca_invoice_type_code = CASE
            WHEN move_type IN ('out_refund', 'in_refund') AND tca_invoice_type_code = '380'    THEN '381'
            WHEN move_type IN ('out_refund', 'in_refund') AND tca_invoice_type_code = '380_sb' THEN '381_sb'
            WHEN move_type IN ('out_refund', 'in_refund') AND tca_invoice_type_code = '480'    THEN '81'
            WHEN move_type IN ('out_invoice', 'in_invoice') AND tca_invoice_type_code = '381'    THEN '380'
            WHEN move_type IN ('out_invoice', 'in_invoice') AND tca_invoice_type_code = '381_sb' THEN '380_sb'
            WHEN move_type IN ('out_invoice', 'in_invoice') AND tca_invoice_type_code = '81'     THEN '480'
            ELSE tca_invoice_type_code
        END
        WHERE tca_invoice_type_code IS NOT NULL
    """)

    # ── 4. BTAE-02 flag-string → 7 boolean fields migration ───────────────
    # Records created before this refactor have tca_transaction_type_flags
    # stored as an 8-char binary string. After this upgrade, the string
    # is COMPUTED from 7 boolean fields. Parse the legacy string into
    # those booleans BEFORE the compute fires — otherwise the compute
    # would overwrite the string with '00000000' (all booleans default
    # to False).
    env.cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'account_move'
          AND column_name = 'tca_transaction_type_flags'
    """)
    if env.cr.fetchone():
        # Use SUBSTRING on the legacy column to set each boolean column.
        # Only operate on rows where the string is exactly 8 chars long.
        env.cr.execute("""
            UPDATE account_move SET
                tca_flag_free_trade_zone   = (SUBSTRING(tca_transaction_type_flags, 1, 1) = '1'),
                tca_flag_deemed_supply     = (SUBSTRING(tca_transaction_type_flags, 2, 1) = '1'),
                tca_flag_margin_scheme     = (SUBSTRING(tca_transaction_type_flags, 3, 1) = '1'),
                tca_flag_summary_invoice   = (SUBSTRING(tca_transaction_type_flags, 4, 1) = '1'),
                tca_flag_continuous_supply = (SUBSTRING(tca_transaction_type_flags, 5, 1) = '1'),
                tca_flag_disclosed_agent   = (SUBSTRING(tca_transaction_type_flags, 6, 1) = '1'),
                tca_flag_ecommerce         = (SUBSTRING(tca_transaction_type_flags, 7, 1) = '1')
            WHERE LENGTH(tca_transaction_type_flags) = 8
        """)
