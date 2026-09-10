package com.example.claims

import com.workfusion.runtime.Execution
import com.workfusion.vault.VaultClient
import org.slf4j.Logger
import org.slf4j.LoggerFactory

/** Policy administration lookups and adjudication rules. */
class PolicyLookup {

    private static final Logger log = LoggerFactory.getLogger(PolicyLookup)

    static void adjudicate(Execution execution) {
        def claim = execution.getVariable('claim')
        def policy = fetchPolicy(claim.policyNumber)

        execution.setVariable('autoApprovalLimit', policy.autoApprovalLimit ?: 10000)

        if (!policy.active) {
            execution.setVariable('outcome', 'DENY')
            execution.setVariable('reason', 'Policy not active on loss date')
            return
        }
        if (execution.getVariable('fraudScore') > 0.75) {
            execution.setVariable('outcome', 'REFER')
            execution.setVariable('reason', 'Fraud score above threshold')
            return
        }
        if (claim.estimatedAmount > policy.perClaimLimit) {
            execution.setVariable('outcome', 'REFER')
            execution.setVariable('reason', 'Exceeds per-claim limit')
            return
        }
        execution.setVariable('outcome', 'APPROVE')
        execution.setVariable('amount', Math.min(claim.estimatedAmount, policy.perClaimLimit))
    }

    static void record(Execution execution) {
        log.info("Claim {} decided: {}",
                execution.getVariable('claim').claimNumber,
                execution.getVariable('outcome'))
    }

    private static fetchPolicy(String policyNumber) {
        // Credentials from the platform vault, never from config.
        def creds = VaultClient.read('secret/claims/policy-admin')
        // ... HTTP call to the policy administration system ...
        return [active: true, perClaimLimit: 100000, autoApprovalLimit: 10000]
    }
}
