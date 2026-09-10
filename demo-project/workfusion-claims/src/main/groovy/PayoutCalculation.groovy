package com.example.claims

import com.workfusion.runtime.Execution
import org.slf4j.Logger
import org.slf4j.LoggerFactory

/** Payout arithmetic and ledger reconciliation. */
class PayoutCalculation {

    private static final Logger log = LoggerFactory.getLogger(PayoutCalculation)

    static void calculate(Execution execution) {
        def claims = execution.getVariable('approvedClaims')
        def payouts = claims.collect { claim ->
            def gross = claim.approvedAmount
            def deductible = claim.policy.deductible ?: 0
            def net = Math.max(gross - deductible, 0)
            // Coinsurance applies only above the deductible.
            if (claim.policy.coinsurance) {
                net = net * (1 - claim.policy.coinsurance)
            }
            [claimNumber: claim.claimNumber, gross: gross,
             deductible: deductible, net: net.setScale(2, BigDecimal.ROUND_HALF_UP)]
        }
        execution.setVariable('payouts', payouts)
        log.info("Calculated {} payout(s)", payouts.size())
    }

    static void reconcile(Execution execution) {
        def payouts = execution.getVariable('payouts')
        def total = payouts.sum { it.net } ?: 0
        log.info("Reconciled {} payouts totalling {}", payouts.size(), total)
    }
}
