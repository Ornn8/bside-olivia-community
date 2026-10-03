from runtime.diagnostics.support_bundle import project_chat_task


def test_quiet_diagnostics_distinguish_proposal_from_execution_without_private_evidence():
    raw = dict(channel='qq', delivery_status='SKIPPED', companion_timing='wait_user',
        companion_decision={'plan': {'proposal': {'timing': 'defer', 'secret': 'private-model-body'}},
                            'source_id_map': {'t1': 'private-id'}},
        skip_reason='USER_REQUESTED_WAIT', user_controls_applied=True,
        silence={'evidence': 'private-user-text'}, followup_quote='private-user-text')
    safe = project_chat_task(raw)
    assert safe == dict(channel='qq', delivery_status='SKIPPED', companion_timing='wait_user',
        companion_proposed_timing='defer', skip_reason='USER_REQUESTED_WAIT', user_controls_applied=True)
    assert project_chat_task(safe) == safe
    assert 'private' not in str(safe)


def test_quiet_diagnostic_values_are_finite_and_typed():
    raw = dict(channel='qq', companion_timing='private-model-text',
        companion_proposed_timing='private-model-text', skip_reason='private-model-text',
        user_controls_applied=1, decision_rejection_reason='private-model-text')
    assert project_chat_task(raw) == {'channel': 'qq'}
